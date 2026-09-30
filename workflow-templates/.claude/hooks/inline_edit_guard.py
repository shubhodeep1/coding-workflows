#!/usr/bin/env python3
"""PreToolUse guard that denies file edits made through an inline interpreter.

Implements the enforced half of CLAUDE.md §23.I ("file edits use the Edit and
Write tools", issue #4858).

Unattended sessions kept editing files with `python3 - <<'EOF' … write_text(…)`
scripts, `python3 -c`, `sed -i`, or `perl -pi`. Claude Code cannot read the
program such a command runs, so no allow rule approves it and the session
stops at a permission prompt only a human can answer. The Edit and Write tools
would have made the same change without a prompt, because none of those files
was a protected path.

This hook answers `permissionDecision: deny` for such a command, with a reason
that redirects to the Edit and Write tools. A deny needs no human: the session
reads the reason and retries with Edit or Write in the same turn.

Denied, when a segment's command word (past assignments, `env`, `sudo`,
`doas`, `timeout N`, `nice`, and the shell keywords) is the interpreter:
  - `python` / `python3` / `pythonX.Y` running a program from `-c` or from a
    heredoc on stdin (`python3 - <<'EOF'`, `python3 <<'EOF'`) that writes:
    `write_text` / `write_bytes`, `open(` (or `fdopen(`, `.open(`) with a
    `w` / `a` / `x` / `+` mode, `os.replace`, `os.rename`, `os.remove`, a
    mutating `shutil` call, or `.unlink(`;
  - `sed -i` / `--in-place`, `perl -i` / `-pi`, `ruby -i`, and
    `awk` / `gawk -i inplace`: those flags exist only to edit files in place.
The same applies to a command inside a substitution Bash runs within one word
(a double-quoted `"$(…)"` or a backtick), which the tokenizer keeps inside
another command's argument.

No decision (the normal permission flow applies):
  - a read-only interpreter program, `pytest`, `python3 -m …`, and a script
    run from a file path (`python3 scripts/x.py`, `python3 .claude/scripts/x.py`);
  - interpreter text that is only data: an argument of another command
    (`git commit -m "…python3 - …"`, `echo`, `grep`) or a quoted string;
  - a program piped in from another command (`cat x | python3 -`), which the
    guard cannot read.

It fails OPEN, unlike `gh_api_write_guard.py`: an unreadable, invalid, or
non-object payload, an unparseable command, or an internal error gives no
decision, with a `systemMessage` warning except for an unparseable command.
Empty input is allowed silently. The shell tokenizer is
`gh_api_write_guard.py`'s own, loaded from the sibling file; if it cannot be
loaded the hook warns and gives no decision.

Signals: every deny writes `INLINE_EDIT_GUARD action=deny kind=<kind>
session=<id>` to stderr and appends one `PermissionDenied` record with
`source: "inline_edit_guard"` to the permission-prompt log through
`permission_prompt_logger.py`. `.claude/scripts/permission_prompts.py` counts
those records as `expected_denies` and never files them. A failure to write
the record never changes the decision.

Kill switch: `CLAUDE_INLINE_EDIT_GUARD=off` (default on) gives no decision for
every command. The hook issues no API calls (§15) and starts no subprocess.

Exit code is always 0 (Claude Code hook protocol); the decision travels in the
JSON `hookSpecificOutput` on stdout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# The exact `.claude/settings.json` matcher this hook is wired under. Kept
# here so the tests can assert the wiring and the hook agree.
SETTINGS_MATCHER = "Bash"

ENV_KILL_SWITCH = "CLAUDE_INLINE_EDIT_GUARD"
LOG_KEY = "INLINE_EDIT_GUARD"
RECORD_SOURCE = "inline_edit_guard"
DECISION_DENY = "deny"

DENY_MESSAGE = (
	"Edit files with the Edit tool (exact old_string/new_string) or the Write tool, not an inline "
	"interpreter. These paths are not protected and will not prompt. For a protected `.claude/**` "
	"file, edit its `workflow-templates/.claude/**` twin instead (CLAUDE.md §28.C twin-first)."
)

_HOOK_DIR = Path(__file__).resolve().parent
_TOKENIZER_PATH = _HOOK_DIR / "gh_api_write_guard.py"
_LOGGER_PATH = _HOOK_DIR / "permission_prompt_logger.py"

# Commands that can never match skip the tokenizer entirely.
_FAST_PATH_RE = re.compile(r"python|perl|ruby|sed|awk")

# The heredoc operator exactly as `strip_heredoc_bodies` finds it, so the Nth
# match in the stripped command is the Nth heredoc it returns.
_HEREDOC_OPERATOR_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")

_PYTHON_RE = re.compile(r"^python(?:[0-9]+(?:\.[0-9]+)*)?$")
_SED_NAMES = frozenset({"sed", "gsed"})
_AWK_NAMES = frozenset({"awk", "gawk"})

# Wrappers that run the command after them, with the options that take a
# separate value. `timeout` also takes a duration before the command, and
# `env` also takes `NAME=VALUE` assignments.
_WRAPPER_VALUE_OPTIONS = {
	"env": frozenset({"-u", "--unset", "-C", "--chdir"}),
	"sudo": frozenset(
		{
			"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "-R",
			"--user", "--group", "--close-from", "--chdir", "--host", "--prompt", "--role", "--type",
			"--other-user", "--command-timeout", "--chroot",
		}
	),
	"doas": frozenset({"-u", "-C"}),
	"timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
	"nice": frozenset({"-n", "--adjustment"}),
}

# Program text that writes, renames, or deletes a file.
_MODE_WRITES = r"[rRbBuU]?(['\"])[rbt]*[wax+][rwaxbt+]*\1"
# One character of a call's arguments, or a parenthesised group nested up to
# three deep, so a comma inside `os.path.join(os.path.dirname(p), 'x')` never
# counts as the `open(` call's own.
_OPEN_ARGUMENT_UNIT = r"(?:[^()]|\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\))"
_PYTHON_WRITE_PATTERNS = (
	re.compile(r"\bwrite_(?:text|bytes)\s*\("),
	re.compile(r"\b(?:fd)?open\s*\(" + _OPEN_ARGUMENT_UNIT + r"*?(?:,|\bmode\s*=)\s*" + _MODE_WRITES),
	re.compile(r"\.open\s*\(\s*(?:mode\s*=\s*)?" + _MODE_WRITES),
	re.compile(r"\bos\.(?:replace|rename|renames|remove)\s*\("),
	re.compile(
		r"\bshutil\.(?:copy|copy2|copyfile|copyfileobj|copytree|copymode|copystat|move|rmtree"
		r"|chown|make_archive|unpack_archive)\s*\("
	),
	re.compile(r"\.unlink\s*\("),
)

# Substitutions nested deeper than this inside one word are left to the normal
# permission flow.
_MAX_SUBSTITUTION_DEPTH = 4

_AWK_INPLACE_VALUES = frozenset({"inplace", "inplace.awk"})
_AWK_VALUE_OPTIONS = frozenset({"-f", "-v", "-F", "-e", "-l", "-E", "--file", "--assign", "--field-separator", "--source", "--load", "--exec"})

_modules: dict[str, object] = {}


def _load_module(name: str, path: Path):
	"""Load a sibling hook file by path (hooks are not a package) and cache it."""
	if name not in _modules:
		spec = importlib.util.spec_from_file_location(name, path)
		if spec is None or spec.loader is None:
			raise ImportError(f"cannot load {path.name}")
		module = importlib.util.module_from_spec(spec)
		# The hook runs from the checkout without PYTHONDONTWRITEBYTECODE, so keep the
		# siblings' bytecode out of .claude/hooks/__pycache__ (CLAUDE.md §13).
		previous = sys.dont_write_bytecode
		sys.dont_write_bytecode = True
		try:
			spec.loader.exec_module(module)
		finally:
			sys.dont_write_bytecode = previous
		_modules[name] = module
	return _modules[name]


def _tokenizer():
	return _load_module("_inline_edit_guard_tokenizer", _TOKENIZER_PATH)


def guard_enabled(environ=None) -> bool:
	"""False only when `CLAUDE_INLINE_EDIT_GUARD` is `off` (any case)."""
	environ = os.environ if environ is None else environ
	return str(environ.get(ENV_KILL_SWITCH, "")).strip().lower() != "off"


def program_writes(program: str) -> bool:
	"""True when Python program text writes, renames, or deletes a file."""
	return any(pattern.search(program) for pattern in _PYTHON_WRITE_PATTERNS)


def _skip_prefix_words(tokens: list[str], index: int, tokenizer) -> int:
	"""Index past the keywords and assignments from `index`, stopping at `env`'s options."""
	start = index
	index += tokenizer._command_word_index(tokens[index:])
	# The tokenizer skips a bare `env` as a prefix word but stops at its first
	# option (`env -i`, `env -u NAME`); step back so the caller skips its options.
	if start < index < len(tokens) and tokens[index - 1] == "env" and tokens[index].startswith("-"):
		index -= 1
	return index


def _command_start(tokens: list[str], tokenizer) -> int:
	"""Index of the command a segment runs, past keywords, assignments, and wrappers."""
	index = _skip_prefix_words(tokens, 0, tokenizer)
	while index < len(tokens):
		word = os.path.basename(tokens[index])
		options = _WRAPPER_VALUE_OPTIONS.get(word)
		if options is None:
			return index
		index += 1
		# A bare `-` ends the options, except for `env`, where it is `-i` (POSIX).
		while index < len(tokens) and tokens[index].startswith("-") and (tokens[index] != "-" or word == "env"):
			if tokens[index] == "--":
				index += 1
				break
			index += 2 if tokens[index] in options else 1
		if word == "timeout" and index < len(tokens):
			index += 1
		index = _skip_prefix_words(tokens, index, tokenizer)
	return index


def python_program(args: list[str]) -> tuple[str, str | None]:
	"""Where a `python` invocation takes its program from.

	Returns `("c", text)` for `-c text`, `("stdin", None)` when the program is
	read from stdin (`-`, or no script at all), and `("other", None)` for a
	module (`-m`), a script path, or a `-c` without its text.
	"""
	index = 0
	while index < len(args):
		arg = args[index]
		index += 1
		if arg == "-":
			return "stdin", None
		if arg == "--":
			return ("other", None) if index < len(args) else ("stdin", None)
		if arg.startswith("--"):
			if arg == "--check-hash-based-pycs":
				index += 1
			continue
		if arg.startswith("-"):
			flags = arg[1:]
			for position, flag in enumerate(flags):
				if flag == "c":
					program = flags[position + 1 :] or (args[index] if index < len(args) else None)
					return ("c", program) if program is not None else ("other", None)
				if flag == "m":
					return "other", None
				if flag in "WX":
					if position == len(flags) - 1:
						index += 1
					break
			continue
		return "other", None
	return "stdin", None


def _sed_in_place(args: list[str]) -> bool:
	index = 0
	while index < len(args):
		arg = args[index]
		index += 1
		if arg == "--":
			return False
		if arg.startswith("--"):
			if arg == "--in-place" or arg.startswith("--in-place="):
				return True
			if arg in ("--expression", "--file", "--line-length"):
				index += 1
			continue
		if arg.startswith("-") and arg != "-":
			letters = arg[1:]
			for position, letter in enumerate(letters):
				if letter == "i":
					return True
				if letter in "efl":
					if position == len(letters) - 1:
						index += 1
					break
		# GNU sed permutes its options, so `sed 's/a/b/' -i file` still counts.
	return False


def _switch_in_place(args: list[str], next_value: str, attached_value: str, digits: str) -> bool:
	"""`-i` in a perl/ruby switch cluster, before the program file.

	`next_value` letters take the next argument when they end the cluster
	(`-e code`), `attached_value` letters take the rest of the cluster
	(`-Idir`), and `digits` letters take following digits (`-0777`, `-l`).
	"""
	index = 0
	while index < len(args):
		arg = args[index]
		index += 1
		if arg == "--" or not arg.startswith("-") or arg == "-":
			return False
		if arg.startswith("--"):
			continue
		letters = arg[1:]
		position = 0
		while position < len(letters):
			letter = letters[position]
			position += 1
			if letter == "i":
				return True
			if letter in next_value:
				if position == len(letters):
					index += 1
				break
			if letter in attached_value:
				break
			if letter in digits:
				if letter == "0" and position < len(letters) and letters[position] in "xX":
					position += 1
					while position < len(letters) and letters[position] in "0123456789abcdefABCDEF":
						position += 1
				while position < len(letters) and letters[position] in "0123456789":
					position += 1
	return False


def _perl_in_place(args: list[str]) -> bool:
	return _switch_in_place(args, next_value="eE", attached_value="IMmxFdDC", digits="0l")


def _ruby_in_place(args: list[str]) -> bool:
	return _switch_in_place(args, next_value="eEIrC", attached_value="FKTWx", digits="0")


def _awk_in_place(args: list[str]) -> bool:
	index = 0
	while index < len(args):
		arg = args[index]
		index += 1
		if arg == "--" or not arg.startswith("-"):
			return False
		value = None
		if arg in ("-i", "--include"):
			value = args[index] if index < len(args) else None
			index += 1
		elif arg.startswith("--include="):
			value = arg.split("=", 1)[1]
		elif arg.startswith("-i"):
			value = arg[2:]
		elif arg in _AWK_VALUE_OPTIONS:
			index += 1
		if value in _AWK_INPLACE_VALUES:
			return True
	return False


def _lines_with_offsets(text: str) -> list[tuple[int, str]]:
	lines: list[tuple[int, str]] = []
	offset = 0
	for line in text.split("\n"):
		lines.append((offset, line))
		offset += len(line) + 1
	return lines


def segment_kind(tokens: list[str], tokenizer) -> str:
	"""The interpreter kind when one command segment edits files in place, else ""."""
	start = _command_start(tokens, tokenizer)
	if start >= len(tokens):
		return ""
	word = os.path.basename(tokens[start])
	args = tokens[start + 1 :]
	if _PYTHON_RE.match(word):
		source, program = python_program(args)
		return "python" if source == "c" and program is not None and program_writes(program) else ""
	if word in _SED_NAMES:
		return "sed" if _sed_in_place(args) else ""
	if word == "perl":
		return "perl" if _perl_in_place(args) else ""
	if word == "ruby":
		return "ruby" if _ruby_in_place(args) else ""
	if word in _AWK_NAMES:
		return "awk" if _awk_in_place(args) else ""
	return ""


def inline_edit_kind(command: str, tokenizer, _depth: int = 0) -> str:
	"""The interpreter kind of the first inline-interpreter file edit in a command, else ""."""
	stripped, heredocs = tokenizer.strip_heredoc_bodies(command)
	try:
		segments = tokenizer.shell_segments(stripped)
	except ValueError:
		return ""
	for tokens in segments:
		kind = segment_kind(tokens, tokenizer)
		if kind:
			return kind
	operator_offsets = [
		line_start + match.start()
		for line_start, line in _lines_with_offsets(stripped)
		for match in _HEREDOC_OPERATOR_RE.finditer(line)
	]
	for (_prefix, _quoted, body), offset in zip(heredocs, operator_offsets):
		# The heredoc feeds the last command before its `<<` operator. Text up
		# to the operator that does not tokenize ends inside an open quote, so
		# the `<<` is data (a commit message, an echo), not a heredoc.
		try:
			prefix_segments = tokenizer.shell_segments(stripped[:offset])
		except ValueError:
			continue
		if not prefix_segments:
			continue
		tokens = prefix_segments[-1]
		start = _command_start(tokens, tokenizer)
		if start >= len(tokens) or not _PYTHON_RE.match(os.path.basename(tokens[start])):
			continue
		source, _program = python_program(tokens[start + 1 :])
		if source == "stdin" and program_writes(body):
			return "python"
	# A double-quoted `$(…)` or a backtick runs its body as a command, but the
	# tokenizer keeps it inside one word of another command, so no segment
	# above starts with it. An unquoted `$(…)` is already its own segment.
	if _depth < _MAX_SUBSTITUTION_DEPTH:
		for substitution in tokenizer.substitution_bodies(stripped):
			kind = inline_edit_kind(substitution, tokenizer, _depth + 1)
			if kind:
				return kind
	return ""


def evaluate(payload: dict, environ=None) -> tuple[str | None, str, str]:
	"""Decide one PreToolUse payload.

	Returns `(decision, reason, kind)`: decision is "deny" or None (no
	decision; the normal permission flow applies), and kind names the
	interpreter (`python`, `sed`, `perl`, `ruby`, `awk`) of a deny.
	"""
	if not guard_enabled(environ):
		return None, "", ""
	if payload.get("tool_name", "Bash") != "Bash":
		return None, "", ""
	tool_input = payload.get("tool_input")
	command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
	if not isinstance(command, str) or not _FAST_PATH_RE.search(command):
		return None, "", ""
	kind = inline_edit_kind(command, _tokenizer())
	if kind:
		return DECISION_DENY, DENY_MESSAGE, kind
	return None, "", ""


def record_deny(payload: dict, kind: str, now: datetime | None = None) -> Path:
	"""Append the deny to the permission-prompt log as a `PermissionDenied` record."""
	logger = _load_module("_inline_edit_guard_logger", _LOGGER_PATH)
	event_payload = dict(payload)
	event_payload["hook_event_name"] = "PermissionDenied"
	event_payload["reason"] = DENY_MESSAGE
	record = logger.build_record(event_payload, now or datetime.now(timezone.utc))
	record["source"] = RECORD_SOURCE
	record["kind"] = kind
	return logger.append_record(record, logger.log_dir())


def _warn(reason: str) -> None:
	"""Emit a non-blocking warning and give no decision."""
	print(json.dumps({"systemMessage": f"Inline-edit guard skipped: {reason}"}))


def main() -> int:
	try:
		raw = sys.stdin.read()
	except (OSError, ValueError):
		_warn("could not read the hook payload")
		return 0
	try:
		payload = json.loads(raw) if raw.strip() else {}
	except ValueError:
		_warn("hook payload is not valid JSON")
		return 0
	if not isinstance(payload, dict):
		_warn("hook payload is not a JSON object")
		return 0

	try:
		decision, reason, kind = evaluate(payload)
	except Exception as exc:  # noqa: BLE001 - fail open: a broken guard must never stop a session
		_warn(f"internal error ({exc})")
		return 0
	if decision != DECISION_DENY:
		return 0

	session = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(payload.get("session_id") or "unknown"))[:120]
	print(f"{LOG_KEY} action=deny kind={kind} session={session}", file=sys.stderr)
	try:
		record_deny(payload, kind)
	except Exception:  # noqa: BLE001 - the log never changes the decision
		pass
	print(
		json.dumps(
			{
				"hookSpecificOutput": {
					"hookEventName": "PreToolUse",
					"permissionDecision": DECISION_DENY,
					"permissionDecisionReason": reason,
				}
			}
		)
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
