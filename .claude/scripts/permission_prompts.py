#!/usr/bin/env python3
"""Report and file the permission prompts a session hit (CLAUDE.md §23.I).

`.claude/hooks/permission_prompt_logger.py` appends one JSON line per
permission prompt (`PermissionRequest`) and Auto-mode denial
(`PermissionDenied`) to `~/.claude/permission-prompts/<session id>.jsonl`.
This script groups those lines into prompt patterns, prints them for the
stage report, and files each new pattern as a GitHub issue so the command or
guard that caused it can be fixed.

Usage:

  permission_prompts.py report [--log-dir DIR]
  permission_prompts.py file [--log-dir DIR] [--session-label ID] [--dry-run]
  permission_prompts.py duplicate-check --repo OWNER/REPO --issue N --target M --fix-pr P

A **pattern** is the event, the tool, and the command's shape: for Bash, each
command word, its flags, subcommand, `gh api` method and endpoint (numbers
and owner/repo replaced), and the shell operators, with every other value
replaced by `*`; for file tools, the tool and the parent directory; for other
tools, the tool name. Its signature is the first 12 hex digits of a SHA-1 of
those three parts.

`file` files only when the local checkout is FILING_REPO: fixes to
`.claude/` land in coding-workflows, because consumer copies are overwritten
on every `@stable` sync. Anywhere else it prints the report and files
nothing. For each pattern with occurrences not filed yet:
  - an `ai:permission-prompt` issue carrying the pattern's marker
    `<!-- ai:permission-prompt:v1 sig=<sig> -->` exists (open or closed) →
    one comment with the new occurrences (a closed issue is not reopened);
  - otherwise → one new issue labelled `ai:permission-prompt` and `ai:claude`,
    so clarify routes it to the Claude issue implementer.
Filed counts are kept in `filed-state.json` next to the logs, so a later run
in the same session files only what is new.

A pattern may also carry a **command class** (`command_class`): one cause
that produces many shapes. The only class is INLINE_INTERPRETER_WRITE_CLASS,
an inline-interpreter write as issue #4858 defines it (`python3 -` with a
heredoc, or `python3 -c`, whose program writes a file; `sed -i`, `perl -i`,
`ruby -i`, `awk -i inplace`). A new issue for a classed pattern carries
`<!-- ai:permission-prompt-class:v1 class=<class> -->`. A new pattern with no
signature match whose class matches an **open** issue carrying that class
marker is added to that issue as a "Seen again" comment instead of a new
issue (issue #4867). A signature match still wins, and a closed class issue
attracts nothing.

`duplicate-check` decides whether an issue-mode session may close issue N as
a duplicate of issue M without asking (CLAUDE.md §23.C carve-out, §23.I
"Closing pipeline-filed duplicates"; issue #4867). It checks conditions 1
and 2: N is open, labelled LABEL, carries the signature marker and
FILED_BY_LINE, its author is the authenticated account, and that author
applied LABEL at creation (`security_pass_skip._label_applied_at_creation`);
M is another issue, open or closed as completed, whose author is an owner,
member, or collaborator; fix PR P is a same-repository PR (its head repository
is its base repository) by an owner, member, or collaborator, references M
(head branch `…issue-<M>-…` or `#<M>` in its title or body), and is open or
merged, and merged into the default branch when M is closed. Anyone can open
an issue or a fork PR in a public repository, so neither counts (issue
#5809). It prints `eligible`, the failed `reasons`, and the evidence fields
(`signature`, `class`, `target_class`, `occurrences`, `target_occurrences`);
the target's two are `null` when its author is not trusted. Evidence that the
cause is the same (condition 3) stays with the session.

Issue text is untrusted data: the tool name; the `**Pattern:**` shape and
the prompt reason, each written as one line with `<!--` escaped (the
signature is taken from the shape before it is escaped); and the command
truncated to MAX_COMMAND_CHARS with heredoc bodies removed and token-like strings masked (REDACTION_PATTERNS),
inside a fenced block longer than any backtick run in it. The markers
(signature, class, "Filed by" line) and the `**Occurrences:**` evidence are
read only outside fenced ````text blocks, so a command that mentions one
neither indexes nor describes the issue.

API calls (CLAUDE.md §15), REST only, none when nothing is new: one read of
the `ai:permission-prompt` issues per 100 issues, then one POST per pattern
filed or commented. The class match reuses that one list read.
`duplicate-check`: at most five GETs (the authenticated user, issue N, one
100-item page of its events, issue M, PR P), none when M equals N.

Prints one JSON line. Exit 0 (including a partial run, whose failures are
listed under `errors`, and a `duplicate-check` that decided either way), 1 on
an invalid argument, 2 when the issue list (or a `duplicate-check` read)
could not be read; exit 1 and 2 of `duplicate-check` print
`"eligible": false`. An argparse usage error (a missing option, or a
non-numeric `--issue`, `--target`, or `--fix-pr`) exits 2 with the usage on
stderr and prints no JSON line, so a caller treats any non-zero exit as
not eligible.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

_CHECKER_PATH = Path(__file__).resolve().with_name("check_in_status.py")
_checker_spec = importlib.util.spec_from_file_location("check_in_status", _CHECKER_PATH)
check_in_status = importlib.util.module_from_spec(_checker_spec)
_checker_spec.loader.exec_module(check_in_status)
_SKIP_CHECK_PATH = Path(__file__).resolve().with_name("security_pass_skip.py")
# Only `duplicate-check` needs this sibling, so a missing or broken copy must
# not stop `report` and `file`: record the error and refuse the check instead.
_SKIP_CHECK_ERROR = ""
try:
	_skip_check_spec = importlib.util.spec_from_file_location("security_pass_skip", _SKIP_CHECK_PATH)
	security_pass_skip = importlib.util.module_from_spec(_skip_check_spec)
	_skip_check_spec.loader.exec_module(security_pass_skip)
except Exception as _skip_check_exc:  # noqa: BLE001 - any load failure disables duplicate-check only
	security_pass_skip = None
	_SKIP_CHECK_ERROR = f"{_SKIP_CHECK_PATH.name} could not be loaded: {_skip_check_exc}"

FILING_REPO = "shubhodeep1/coding-workflows"
LABEL = "ai:permission-prompt"
ROUTE_LABEL = "ai:claude"
MARKER_TEMPLATE = "<!-- ai:permission-prompt:v1 sig={sig} -->"
MARKER_RE = re.compile(r"<!-- ai:permission-prompt:v1 sig=([0-9a-f]{12}) -->")
FILED_BY_LINE = "Filed by `.claude/scripts/permission_prompts.py`"
INLINE_INTERPRETER_WRITE_CLASS = "inline-interpreter-write"
CLASS_MARKER_TEMPLATE = "<!-- ai:permission-prompt-class:v1 class={command_class} -->"
CLASS_MARKER_RE = re.compile(r"<!-- ai:permission-prompt-class:v1 class=([a-z][a-z0-9-]{0,60}) -->")
OCCURRENCES_RE = re.compile(r"^\*\*Occurrences:\*\* .+$", re.MULTILINE)
# A fenced ````text block of an issue body: the session's command, untrusted
# data that can itself contain marker text. Markers are read only outside it.
# The filer opens it with four backticks, or one more than the longest backtick
# run in the example (`_example_fence`), and closes it with the same run, so a
# backtick line inside the example never ends it early.
_FENCED_EXAMPLE_RE = re.compile(r"^(`{4,})text\n.*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
_BACKTICK_RUN_RE = re.compile(r"`+")
DUPLICATE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# A program that writes a file (issue #4858 item 1): a write call, `open(`
# with a write mode, or a file move/removal. The mode is the argument after a
# comma or `mode=` (`open(p, "w")`), or `Path.open`'s first one
# (`.open("a")`), never a file name such as `open("a")`. `open(` calls are
# scanned by `_open_call_writes`, which tracks brackets and string literals, so
# the path argument may nest calls to any depth
# (`open(os.path.join(str(p.replace("/", "_")), "f"), "w")`) and a comma inside
# one of them (`open(os.path.join(d, "w"))`) is not the mode's comma.
_OPEN_WRITE_MODE = r"['\"](?:[wax]|r[bt]?\+)[bt+]*['\"]"
_OPEN_CALL_RE = re.compile(r"\bopen\s*\(")
# The mode at the start of an `open(` argument: `mode=` names it anywhere, a
# bare literal only after the first comma.
_OPEN_KEYWORD_MODE_RE = re.compile(r"\s*mode\s*=\s*" + _OPEN_WRITE_MODE)
_OPEN_MODE_ARGUMENT_RE = re.compile(r"\s*(?:mode\s*=\s*)?" + _OPEN_WRITE_MODE)
_PROGRAM_WRITE_RE = re.compile(
	r"\bwrite_text\s*\(|\bwrite_bytes\s*\("
	r"|\.open\s*\(\s*" + _OPEN_WRITE_MODE +
	r"|\bos\.replace\s*\("
	r"|\bshutil\.(?:copy(?:file|2|tree|mode|stat)?|move|rmtree|chown|make_archive|unpack_archive)\s*\("
	r"|\.unlink\s*\(|\bos\.remove\s*\("
)
_PYTHON_RE = re.compile(r"^python(?:3(?:\.[0-9]+)?)?$")
# `-c`, alone or after argument-less switches (`-Ic`, `-uc`, `-IBc`).
_PYTHON_C_RE = re.compile(r"^-[bBdEiIOPqRsSuv]*c$")
# `-m`, alone or bundled, with or without its module attached (`-m`, `-Im`,
# `-mpytest`): the module runs, so no inline program follows.
_PYTHON_M_RE = re.compile(r"^-[bBdEiIOPqRsSuv]*m")
# Interpreter switches whose value is the next argument, not an operand.
_PYTHON_VALUE_SWITCHES = frozenset({"-W", "-X", "--check-hash-based-pycs"})
# In-place edit switches: `sed -i`/`-i.bak`/`--in-place`; `perl`/`ruby` `-i`
# alone or after argument-less switches (`-pi`, `-pi.bak`, `-lpi`).
_SED_IN_PLACE_RE = re.compile(r"^(?:-[A-Za-z]*i.*|--in-place(?:=.*)?)$")
_PERL_IN_PLACE_RE = re.compile(r"^-[pnlaswtTcvW0-9]*i.*$")
_RUBY_IN_PLACE_RE = re.compile(r"^-[pnlaswvWcd]*i.*$")
STATE_FILE = "filed-state.json"
DEFAULT_LOG_DIR = Path.home() / ".claude" / "permission-prompts"
MAX_COMMAND_CHARS = 2000
MAX_SHAPE_CHARS = 200
MAX_TITLE_SHAPE_CHARS = 90

REDACTION_PATTERNS = (
	(re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "gh*_***"),
	(re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_***"),
	(re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-***"),
	(re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), "xox*-***"),
	(re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
	(re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
	(re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key)(\s*[=:]\s*)[^\s'\"]+"), r"\1\2***"),
	# Long random-looking strings (hex keys, base64 secrets): 40+ letters,
	# digits, `+`, `_`, `=` with both letters and digits. `/` and `-` are left
	# out so API paths and branch names survive.
	(re.compile(r"(?<![A-Za-z0-9+_=])(?=[A-Za-z0-9+_=]*[0-9])(?=[A-Za-z0-9+_=]*[A-Za-z])[A-Za-z0-9+_=]{40,}"), "***"),
)

_SHELL_PUNCTUATION_CHARS = ";&|\n<>()"
_HEREDOC_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_SUBCOMMAND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
# Commands whose first argument is a subcommand worth keeping in a shape
# (`gh api`, `git push`); for anything else the first argument is a value.
_SUBCOMMAND_TOOLS = frozenset(
	{"gh", "git", "npm", "npx", "pnpm", "yarn", "docker", "kubectl", "doctl", "wrangler", "cargo", "go", "pip", "pip3", "uv", "make", "terraform", "gcloud", "aws", "az"}
)
_SCRIPT_RE = re.compile(r"\.(py|sh|js|mjs|ts)$")
_FILE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
_BULKY_INPUT_KEYS = frozenset({"content", "new_string", "old_string", "new_source", "edits"})


def strip_heredocs(command: str, placeholder: str = "", keep_delimiter: bool = True) -> str:
	"""Remove heredoc bodies, keeping the `<<WORD` operators (and, by default, the delimiter lines)."""
	lines = command.split("\n")
	output: list[str] = []
	index = 0
	while index < len(lines):
		line = lines[index]
		output.append(line)
		index += 1
		for match in _HEREDOC_RE.finditer(line):
			delimiter, strip_tabs = match.group(3), match.group(1) == "-"
			skipped = 0
			terminated = False
			while index < len(lines):
				body_line = lines[index]
				index += 1
				if (body_line.lstrip("\t") if strip_tabs else body_line) == delimiter:
					terminated = True
					break
				skipped += 1
			if placeholder and skipped:
				output.append(placeholder)
			if terminated and keep_delimiter:
				output.append(delimiter)
	return "\n".join(output)


def redact(text: str) -> str:
	for pattern, replacement in REDACTION_PATTERNS:
		text = pattern.sub(replacement, text)
	return text


def _normalize_endpoint(endpoint: str) -> str:
	path, _, query = endpoint.lstrip("/").partition("?")
	path = re.sub(r"^repos/[^/]+/[^/]+", "repos/*/*", path)
	path = re.sub(r"\b[0-9]+\b", "N", path)
	return path + ("?*" if query else "")


def _segment_shape(tokens: list[str]) -> list[str]:
	shape: list[str] = []
	index = 0
	while index < len(tokens) and _ASSIGNMENT_RE.match(tokens[index]):
		shape.append(_ASSIGNMENT_RE.match(tokens[index]).group(1) + "=*")
		index += 1
	if index >= len(tokens):
		return shape
	command = tokens[index].rsplit("/", 1)[-1] if not tokens[index].startswith(".") else tokens[index]
	shape.append(command)
	positionals = 0
	seen_flags: set[str] = set()
	index += 1
	while index < len(tokens):
		token = tokens[index]
		index += 1
		if token.startswith("\x00"):
			shape.append(token[1:])
			continue
		if token.startswith("-") and len(token) > 1:
			flag = token.split("=", 1)[0] + ("=*" if "=" in token else "")
			if flag in ("-X", "--method") and index < len(tokens):
				flag = f"{flag} {tokens[index].upper()}"
				index += 1
			if flag not in seen_flags:
				seen_flags.add(flag)
				shape.append(flag)
			continue
		positionals += 1
		if command == "gh" and positionals == 2 and len(shape) >= 2 and shape[1] == "api":
			shape.append(_normalize_endpoint(token))
		elif positionals == 1 and ((command in _SUBCOMMAND_TOOLS and _SUBCOMMAND_RE.match(token)) or _SCRIPT_RE.search(token)):
			shape.append(token)
		elif shape[-1] != "*":
			shape.append("*")
	return shape


def command_shape(command: str) -> str:
	"""Return the Bash command's shape: structure kept, literal values replaced by `*`."""
	stripped = strip_heredocs(command, keep_delimiter=False)
	lexer = shlex.shlex(stripped.replace("\\\n", " "), posix=True, punctuation_chars=_SHELL_PUNCTUATION_CHARS)
	lexer.commenters = ""
	lexer.whitespace = " \t\r"
	lexer.whitespace_split = True
	try:
		tokens = list(lexer)
	except ValueError:
		return "unparseable: " + re.sub(r"[0-9]+", "N", stripped.split("\n", 1)[0])[:60]
	parts: list[str] = []
	segment: list[str] = []
	redirect_target = False
	for token in tokens:
		is_punctuation = bool(token) and set(token) <= set(_SHELL_PUNCTUATION_CHARS)
		if redirect_target and not is_punctuation:
			# A redirect target (`> $S/body.md`, `<<'EOF'`) is a value, not a command.
			segment.append("\x00*")
			redirect_target = False
			continue
		redirect_target = False
		if is_punctuation and set(token) <= set("<>&") and set(token) & set("<>"):
			if segment and segment[-1].isdigit():
				token = segment.pop() + token
			segment.append("\x00" + token)
			redirect_target = True
			continue
		if is_punctuation:
			parts.extend(_segment_shape(segment))
			segment = []
			parts.append(";" if token == "\n" else token)
			continue
		segment.append(token)
	parts.extend(_segment_shape(segment))
	return " ".join(parts).strip()[:MAX_SHAPE_CHARS]


def heredoc_bodies(command: str) -> list[str]:
	"""The body text of every heredoc in `command` (the part `strip_heredocs` removes)."""
	lines = command.split("\n")
	bodies: list[str] = []
	index = 0
	while index < len(lines):
		line = lines[index]
		index += 1
		for match in _HEREDOC_RE.finditer(line):
			delimiter, strip_tabs = match.group(3), match.group(1) == "-"
			body: list[str] = []
			while index < len(lines):
				body_line = lines[index]
				index += 1
				if (body_line.lstrip("\t") if strip_tabs else body_line) == delimiter:
					break
				body.append(body_line)
			bodies.append("\n".join(body))
	return bodies


def _command_segments(command: str) -> list[list[str]]:
	"""Split a Bash command (heredoc bodies removed) into simple commands; redirects stay in their segment."""
	stripped = strip_heredocs(command, keep_delimiter=False)
	lexer = shlex.shlex(stripped.replace("\\\n", " "), posix=True, punctuation_chars=_SHELL_PUNCTUATION_CHARS)
	lexer.commenters = ""
	lexer.whitespace = " \t\r"
	lexer.whitespace_split = True
	try:
		tokens = list(lexer)
	except ValueError:
		return []
	segments: list[list[str]] = [[]]
	for token in tokens:
		is_punctuation = bool(token) and set(token) <= set(_SHELL_PUNCTUATION_CHARS)
		if is_punctuation and not (set(token) <= set("<>&") and set(token) & set("<>")):
			segments.append([])
			continue
		segments[-1].append(token)
	return [segment for segment in segments if segment]


def _is_redirect(token: str) -> bool:
	return bool(token) and set(token) <= set("<>&") and bool(set(token) & set("<>"))


def _is_heredoc_operator(token: str) -> bool:
	"""A `<<` heredoc operator token; a `<<<` herestring has no heredoc body.

	No `<<-` token reaches this: the lexer splits `<<-WORD` into `<<` and
	`-WORD`, so a `<<-` heredoc is counted through its `<<` token.
	"""
	return _is_redirect(token) and token.startswith("<<") and not token.startswith("<<<")


def _split_redirects(args: list[str]) -> tuple[list[str], list[str]]:
	"""Return (arguments, redirect operators): each operator's target and an fd number before it are dropped."""
	kept: list[str] = []
	redirects: list[str] = []
	index = 0
	while index < len(args):
		arg = args[index]
		if _is_redirect(arg):
			redirects.append(arg)
			if kept and kept[-1].isdigit():
				kept.pop()
			index += 2
			continue
		kept.append(arg)
		index += 1
	return kept, redirects


def _open_call_writes(text: str) -> bool:
	"""Whether an `open(` call in `text` passes a write mode, at any nesting depth of its arguments."""
	for call in _OPEN_CALL_RE.finditer(text):
		mode_pattern = _OPEN_KEYWORD_MODE_RE
		index = call.end()
		depth = 0
		quote = ""
		while index < len(text):
			if mode_pattern is not None:
				if mode_pattern.match(text, index):
					return True
				mode_pattern = None
			char = text[index]
			index += 1
			if quote:
				if char == "\\":
					index += 1
				elif char == quote:
					quote = ""
			elif char in "'\"":
				quote = char
			elif char in "([{":
				depth += 1
			elif char in ")]}":
				if depth == 0:
					break
				depth -= 1
			elif char == "," and depth == 0:
				mode_pattern = _OPEN_MODE_ARGUMENT_RE
	return False


def _program_writes(text: str) -> bool:
	"""Whether a program's text writes a file (`_PROGRAM_WRITE_RE` or a write-mode `open(`)."""
	return bool(_PROGRAM_WRITE_RE.search(text)) or _open_call_writes(text)


def _segment_is_inline_write(segment: list[str], segment_bodies: list[str]) -> bool:
	"""Whether one simple command is an inline-interpreter write; `segment_bodies` are its own heredoc bodies."""
	index = 0
	while index < len(segment) and _ASSIGNMENT_RE.match(segment[index]):
		index += 1
	if index >= len(segment):
		return False
	name = segment[index].rsplit("/", 1)[-1]
	args, redirects = _split_redirects(segment[index + 1 :])
	if _PYTHON_RE.match(name):
		# Only the interpreter's own switches, before the first operand: in
		# `python3 tool.py -c X` or `python3 -m mod -` the `-c` / `-` belong to
		# the script or module, so no inline program runs. After `--` every
		# argument is an operand: `python3 -- -c X` runs a file named `-c`.
		position = 0
		while position < len(args):
			arg = args[position]
			if arg == "--":
				position += 1
				break
			if _PYTHON_C_RE.match(arg):
				return position + 1 < len(args) and _program_writes(args[position + 1])
			if arg == "-" or not arg.startswith("-") or _PYTHON_M_RE.match(arg):
				break
			position += 2 if arg in _PYTHON_VALUE_SWITCHES else 1
		reads_stdin = position >= len(args) or args[position] == "-"
		has_heredoc = any(_is_heredoc_operator(redirect) for redirect in redirects)
		return reads_stdin and has_heredoc and any(_program_writes(body) for body in segment_bodies)
	if name == "sed":
		return any(_SED_IN_PLACE_RE.match(arg) for arg in args)
	if name == "perl":
		return any(_PERL_IN_PLACE_RE.match(arg) for arg in args)
	if name == "ruby":
		return any(_RUBY_IN_PLACE_RE.match(arg) for arg in args)
	if name in ("awk", "gawk"):
		pairs = zip(args, args[1:])
		return "-iinplace" in args or "--include=inplace" in args or any(flag in ("-i", "--include") and value == "inplace" for flag, value in pairs)
	return False


def command_class(record: dict) -> str:
	"""The pattern's command class (see the module docstring), or "" when it has none."""
	if record.get("tool_name") != "Bash":
		return ""
	tool_input = record.get("tool_input") if isinstance(record.get("tool_input"), dict) else {}
	command = str(tool_input.get("command") or "")
	segments = _command_segments(command)
	bodies = heredoc_bodies(command)
	# Heredoc bodies follow their `<<` operators in order, so each segment gets
	# only its own (a write in `cat <<EOF` after a reading `python3 - <<EOF` is
	# not the interpreter's). A `<<<` herestring has no body and is not
	# counted. When the counts disagree (a `<<` inside quotes), no body can be
	# bound to its command, so none is: the pattern is then filed on its own
	# signature rather than routed to a class issue.
	counts = [sum(1 for token in segment if _is_heredoc_operator(token)) for segment in segments]
	offsets = [sum(counts[:position]) for position in range(len(segments))]
	aligned = sum(counts) == len(bodies)
	for segment, count, offset in zip(segments, counts, offsets):
		if _segment_is_inline_write(segment, bodies[offset : offset + count] if aligned else []):
			return INLINE_INTERPRETER_WRITE_CLASS
	return ""


def record_shape(record: dict) -> str:
	tool = record.get("tool_name") or ""
	tool_input = record.get("tool_input") if isinstance(record.get("tool_input"), dict) else {}
	if tool == "Bash":
		return command_shape(str(tool_input.get("command") or ""))
	if tool in _FILE_TOOLS:
		path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
		cwd = str(record.get("cwd") or "")
		if cwd and path.startswith(cwd.rstrip("/") + "/"):
			path = path[len(cwd.rstrip("/")) + 1 :]
		parent = path.rsplit("/", 1)[0] if "/" in path else "."
		return f"{parent}/*"
	return ""


def signature(event: str, tool: str, shape: str) -> str:
	return hashlib.sha1(f"{event}\n{tool}\n{shape}".encode("utf-8")).hexdigest()[:12]


def record_example(record: dict) -> str:
	"""The redacted, truncated command (or tool input) for the issue text."""
	tool_input = record.get("tool_input") if isinstance(record.get("tool_input"), dict) else {}
	if record.get("tool_name") == "Bash":
		text = strip_heredocs(str(tool_input.get("command") or ""), placeholder="<heredoc body omitted>")
	else:
		shown = {
			key: (f"<{len(value) if isinstance(value, str) else 'n'} chars omitted>" if key in _BULKY_INPUT_KEYS else value)
			for key, value in tool_input.items()
		}
		text = json.dumps(shown, ensure_ascii=False, indent=1)
	text = redact(text)
	if len(text) > MAX_COMMAND_CHARS:
		text = text[:MAX_COMMAND_CHARS] + "\n… [truncated]"
	return text


def load_records(log_dir: Path) -> list[dict]:
	records: list[dict] = []
	if not log_dir.is_dir():
		return records
	for path in sorted(log_dir.glob("*.jsonl")):
		try:
			lines = path.read_text(encoding="utf-8").splitlines()
		except OSError:
			continue
		for line in lines:
			try:
				record = json.loads(line)
			except ValueError:
				continue
			if isinstance(record, dict) and record.get("event") in ("PermissionRequest", "PermissionDenied"):
				records.append(record)
	return records


def group_patterns(records: list[dict]) -> list[dict]:
	"""Group records into patterns, ordered by first occurrence."""
	patterns: dict[str, dict] = {}
	for record in records:
		event = str(record.get("event"))
		tool = str(record.get("tool_name") or "")
		shape = record_shape(record)
		sig = signature(event, tool, shape)
		pattern = patterns.get(sig)
		if pattern is None:
			pattern = patterns[sig] = {
				"signature": sig,
				"event": event,
				"tool_name": tool,
				"shape": shape,
				"count": 0,
				"reasons": [],
				"first_ts": record.get("ts"),
				"last_ts": record.get("ts"),
				"example": "",
				"class": "",
			}
		pattern["class"] = pattern["class"] or command_class(record)
		pattern["count"] += 1
		pattern["last_ts"] = record.get("ts")
		pattern["example"] = record_example(record)
		reason = str(record.get("reason") or "")
		if reason and reason not in pattern["reasons"] and len(pattern["reasons"]) < 3:
			pattern["reasons"].append(redact(reason)[:300])
	return list(patterns.values())


def report(log_dir: Path) -> dict:
	patterns = group_patterns(load_records(log_dir))
	return {
		"total": sum(pattern["count"] for pattern in patterns),
		"patterns": [{key: pattern[key] for key in ("signature", "event", "tool_name", "shape", "count", "reasons", "class")} for pattern in patterns],
	}


def _event_label(event: str) -> str:
	return "permission prompt" if event == "PermissionRequest" else "Auto-mode denial"


def issue_title(pattern: dict) -> str:
	subject = pattern["shape"] or pattern["tool_name"]
	if len(subject) > MAX_TITLE_SHAPE_CHARS:
		subject = subject[: MAX_TITLE_SHAPE_CHARS - 1] + "…"
	return f"[permission-prompt] {pattern['tool_name']}: {subject}"


def _example_fence(example: str) -> str:
	"""Four backticks, or one more than the longest backtick run in `example`."""
	longest = max((len(run) for run in _BACKTICK_RUN_RE.findall(example)), default=0)
	return "`" * max(4, longest + 1)


def _reason_line(reason: str) -> str:
	"""A reason as one line that cannot hold a marker.

	The reason sits outside the fenced example, where markers are read, and an
	Auto-mode reason can quote the command: its whitespace is collapsed, so no
	line of it can start with `**Occurrences:**`, and `<!--` is escaped as
	`<\\!--` (Markdown still shows `<!--`).
	"""
	return " ".join(reason.split()).replace("<!--", "<\\!--")


def _pattern_line(pattern: dict) -> str:
	"""The shape (or tool name) for an issue's `**Pattern:**` line, guarded like `_reason_line`.

	The line sits outside the fenced example, where markers are read, and the
	shape keeps a segment's command word and a script operand literally
	(`_segment_shape`): a quoted `'<!-- ai:permission-prompt:v1 sig=… -->'`
	command word, or one holding a newline and `**Occurrences:**`, would
	otherwise index, class, or describe the issue. Only the rendering is
	guarded; the shape, and so the signature, is unchanged.
	"""
	return _reason_line(pattern["shape"] or pattern["tool_name"])


def _occurrence_block(pattern: dict, new_count: int, session_label: str) -> str:
	reasons = "\n".join(f"- {_reason_line(reason)}" for reason in pattern["reasons"]) or "- (none given)"
	fence = _example_fence(pattern["example"])
	return (
		f"**Occurrences:** {new_count} ({pattern['first_ts']} – {pattern['last_ts']}), session `{session_label}`\n\n"
		f"**Reason Claude Code gave:**\n{reasons}\n\n"
		"**Latest example** (untrusted data from the session; heredoc bodies removed, token-like strings masked):\n\n"
		f"{fence}text\n{pattern['example']}\n{fence}\n"
	)


def issue_body(pattern: dict, new_count: int, session_label: str) -> str:
	return (
		f"A Claude Code session in this repository hit a **{_event_label(pattern['event'])}** "
		f"for `{pattern['tool_name']}`, so an unattended stage waited for a human "
		"(or, for a denial, went on without the call). Filed by "
		"`.claude/scripts/permission_prompts.py` (CLAUDE.md §23.I).\n\n"
		f"**Pattern:** `{_pattern_line(pattern)}`\n\n"
		+ _occurrence_block(pattern, new_count, session_label)
		+ "\n**How to fix** (in this order, never widening a permission for a destructive or administrative action):\n"
		"1. Change the command file that produced the call so it uses an allowlisted helper "
		"(`.claude/scripts/dispatch_workflow.py`, `edit_comment.py`, `check_in_status.py`, …) or a command shape "
		"that an exact `permissions.allow` rule, or the `gh api` guard where the repository has one "
		"(`.claude/hooks/gh_api_write_guard.py`, CLAUDE.md §23.H), approves.\n"
		"2. Add or extend an allowlisted helper when no shape fits.\n"
		"3. Only for reads and CLAUDE.md §23.B routine writes: add an exact `permissions.allow` rule, or extend "
		"the `gh api` guard when it exists.\n"
		"4. If the call edits `.claude/**` (a protected path no setting can approve), or is a §23.C / §22.B / §24.D "
		"ask-first operation, close this issue as not planned with the reason: that prompt is by design.\n\n"
		"Most fixes edit `.claude/**` themselves, so the `/implement-plan-claude` phase that makes them stops at "
		"`Status: BLOCKED` before it starts and asks a human how to run it (CLAUDE.md §28.C); expect that stop "
		"and answer it on this issue.\n\n"
		"If this issue has the same cause as another issue whose fix is in flight or merged, the "
		"`/implement-issue-claude` session closes it as a duplicate itself when "
		"`permission_prompts.py duplicate-check` allows it (CLAUDE.md §23.I, \"Closing pipeline-filed "
		"duplicates\"); otherwise it asks here.\n\n"
		+ (CLASS_MARKER_TEMPLATE.format(command_class=pattern["class"]) + "\n" if pattern.get("class") else "")
		+ MARKER_TEMPLATE.format(sig=pattern["signature"])
		+ "\n"
	)


def comment_body(pattern: dict, new_count: int, session_label: str) -> str:
	return f"Seen again.\n\n{_occurrence_block(pattern, new_count, session_label)}"


def class_comment_body(pattern: dict, new_count: int, session_label: str) -> str:
	"""The comment for a new pattern routed to an open issue of the same command class."""
	return (
		f"Seen again: a new pattern of the same command class (`{pattern['class']}`), added here instead of "
		"a new issue (CLAUDE.md §23.I).\n\n"
		f"**Pattern:** `{pattern['shape'] or pattern['tool_name']}` (`{pattern['tool_name']}`, "
		f"{_event_label(pattern['event'])}, signature `{pattern['signature']}`)\n\n"
		+ _occurrence_block(pattern, new_count, session_label)
	)


def extract_repo_slug(url: str) -> str:
	"""`<owner>/<repo>` from a git remote URL (github.com or Claude Code Web's local proxy), or ""."""
	url = url.strip()
	for suffix in ("/", ".git", "/"):
		if url.endswith(suffix):
			url = url[: -len(suffix)]
	if "://" in url:
		rest = url.split("://", 1)[1]
		if "@" in rest:
			rest = rest.rsplit("@", 1)[1]
		if "/" not in rest:
			return ""
		host, path = rest.split("/", 1)
		host = host.split(":", 1)[0]
	elif "@" in url and ":" in url.split("@", 1)[1]:
		host, path = url.rsplit("@", 1)[1].split(":", 1)
	else:
		return ""
	if host in ("127.0.0.1", "localhost"):
		if not path.startswith("git/"):
			return ""
		path = path[len("git/") :]
	elif host != "github.com":
		return ""
	return path if re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9._-]+", path) else ""


def local_repo_slug() -> str:
	try:
		proc = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, timeout=5)
	except (OSError, subprocess.SubprocessError):
		return ""
	return extract_repo_slug(proc.stdout) if proc.returncode == 0 else ""


def _post(path: str, body: dict) -> dict:
	payload_file = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
	payload_path = payload_file.name
	try:
		with payload_file:
			json.dump(body, payload_file)
		proc = subprocess.run(["gh", "api", "-X", "POST", path, "--input", payload_path], capture_output=True, text=True, timeout=60)
	except (OSError, TypeError, ValueError, subprocess.TimeoutExpired) as exc:
		raise check_in_status.ReadError(f"POST {path} failed: {exc}") from exc
	finally:
		Path(payload_path).unlink(missing_ok=True)
	if proc.returncode != 0:
		detail = (proc.stderr or proc.stdout).strip().splitlines()
		raise check_in_status.ReadError(f"POST {path} failed: {detail[-1] if detail else f'exit {proc.returncode}'}")
	try:
		created = json.loads(proc.stdout)
	except ValueError:
		created = {}
	return created if isinstance(created, dict) else {}


def _load_state(log_dir: Path) -> dict[str, int]:
	try:
		state = json.loads((log_dir / STATE_FILE).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return {key: value for key, value in state.items() if isinstance(key, str) and isinstance(value, int)} if isinstance(state, dict) else {}


def _save_state(log_dir: Path, state: dict[str, int]) -> None:
	log_dir.mkdir(parents=True, exist_ok=True)
	(log_dir / STATE_FILE).write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def _outside_fenced_examples(body: str) -> str:
	"""`body` without its fenced ````text blocks, where the filer's markers are read.

	A command example that mentions `<!-- ai:permission-prompt:v1 sig=… -->`, a
	class marker, the "Filed by" line, or an `**Occurrences:**` line (a session
	grepping for one, say) must not index, class, or describe the issue it is
	filed on.
	"""
	return _FENCED_EXAMPLE_RE.sub("", body)


def list_permission_prompt_issues(slug: str) -> list[dict]:
	"""Every `ai:permission-prompt` issue, open or closed (1 REST read per 100)."""
	return check_in_status.gh_api_list(f"repos/{slug}/issues?labels={LABEL.replace(':', '%3A')}&state=all")


def index_by_signature(issues: list[dict]) -> dict[str, dict]:
	"""Map signature → {number, state}; the first issue listed with a marker wins."""
	found: dict[str, dict] = {}
	for issue in issues:
		if issue.get("pull_request"):
			continue
		match = MARKER_RE.search(_outside_fenced_examples(issue.get("body") or ""))
		if match and match.group(1) not in found:
			found[match.group(1)] = {"number": issue.get("number"), "state": issue.get("state")}
	return found


def open_issues_by_class(issues: list[dict]) -> dict[str, int]:
	"""Map command class → the lowest-numbered **open** issue carrying that class marker."""
	found: dict[str, int] = {}
	for issue in issues:
		number = issue.get("number")
		if issue.get("pull_request") or issue.get("state") != "open" or not isinstance(number, int):
			continue
		match = CLASS_MARKER_RE.search(_outside_fenced_examples(issue.get("body") or ""))
		if match and (match.group(1) not in found or number < found[match.group(1)]):
			found[match.group(1)] = number
	return found


def existing_issues(slug: str) -> dict[str, dict]:
	"""Map signature → {number, state} for every `ai:permission-prompt` issue (1 REST read per 100)."""
	return index_by_signature(list_permission_prompt_issues(slug))


def file_patterns(log_dir: Path, session_label: str, dry_run: bool, slug: str | None = None) -> tuple[int, dict]:
	"""File new patterns as issues or comments; see the module docstring."""
	slug = local_repo_slug() if slug is None else slug
	patterns = group_patterns(load_records(log_dir))
	summary = report(log_dir)
	if slug.lower() != FILING_REPO:
		summary.update({"filed": [], "commented": [], "errors": [], "skipped": f"filing is limited to {FILING_REPO}; this checkout is {slug or 'unknown'}"})
		return 0, summary
	state = _load_state(log_dir)
	pending = [(pattern, pattern["count"] - state.get(pattern["signature"], 0)) for pattern in patterns]
	pending = [(pattern, new_count) for pattern, new_count in pending if new_count > 0]
	summary.update({"filed": [], "commented": [], "errors": []})
	if not pending:
		return 0, summary
	try:
		listed = list_permission_prompt_issues(slug)
	except check_in_status.ReadError as exc:
		summary["errors"].append(str(exc))
		return 2, summary
	existing = index_by_signature(listed)
	class_issues = open_issues_by_class(listed)
	for pattern, new_count in pending:
		sig = pattern["signature"]
		pattern_class = pattern.get("class") or ""
		try:
			if sig in existing:
				number = existing[sig]["number"]
				if not dry_run:
					_post(f"repos/{slug}/issues/{number}/comments", {"body": comment_body(pattern, new_count, session_label)})
				summary["commented"].append({"signature": sig, "issue": number, "occurrences": new_count})
			elif pattern_class and pattern_class in class_issues:
				number = class_issues[pattern_class]
				if not dry_run:
					_post(f"repos/{slug}/issues/{number}/comments", {"body": class_comment_body(pattern, new_count, session_label)})
				summary["commented"].append({"signature": sig, "issue": number, "occurrences": new_count, "class": pattern_class})
			else:
				created = {} if dry_run else _post(
					f"repos/{slug}/issues",
					{"title": issue_title(pattern), "body": issue_body(pattern, new_count, session_label), "labels": [LABEL, ROUTE_LABEL]},
				)
				summary["filed"].append({"signature": sig, "issue": created.get("number"), "title": issue_title(pattern)})
				if created.get("number"):
					existing[sig] = {"number": created.get("number"), "state": "open"}
					if pattern_class and isinstance(created.get("number"), int):
						class_issues.setdefault(pattern_class, created["number"])
		except check_in_status.ReadError as exc:
			summary["errors"].append(str(exc))
			continue
		if not dry_run:
			state[sig] = pattern["count"]
			_save_state(log_dir, state)
	summary["dry_run"] = dry_run
	return 0, summary


def _first_match(pattern: re.Pattern[str], text: str, group: int = 0) -> str | None:
	match = pattern.search(text)
	return match.group(group) if match else None


def _references_issue(pull: dict, number: int) -> bool:
	head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
	if f"issue-{number}-" in str(head.get("ref") or ""):
		return True
	text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
	return bool(re.search(rf"#{number}(?![0-9])", text))


def _repo_full_name(ref: object) -> str:
	"""`repo.full_name` of a pull request's `head` or `base`, or "" (a deleted fork's head has no repo)."""
	repo = ref.get("repo") if isinstance(ref, dict) else None
	return str(repo.get("full_name") or "") if isinstance(repo, dict) else ""


def _untrusted_author(item: dict) -> str:
	"""`<login> (<association>)` of an issue or pull request author, for a refusal reason."""
	login = security_pass_skip._login(item.get("user")) or "unknown"
	return f"{login} ({item.get('author_association') or 'no association'})"


def decide_duplicate_close(
	issue: dict,
	events: list | None,
	target: dict,
	fix_pr: dict,
	login: str,
	issue_number: int,
	target_number: int,
) -> dict:
	"""Pure decision over already-fetched data for `duplicate-check` (see the module docstring).

	`issue` and `target` are REST issue objects, `events` the first 100-item
	page of the issue's events (None when unreadable), `fix_pr` the REST pull
	request, and `login` the authenticated account. Returns `eligible`,
	`reasons` (every failed check), and the evidence fields. Trust comes from
	`author_association` (`check_in_status.FIX_CLAIM_TRUSTED_ASSOCIATIONS`) and
	the PR's head and base `repo.full_name`, all in the objects already read; a
	missing field is untrusted.
	"""
	reasons: list[str] = []
	body = str(issue.get("body") or "")
	markers = _outside_fenced_examples(body)
	# Issue #5809: anyone can open an issue in a public repository, so the
	# target's body (and its markers) count only when a trusted account wrote it.
	target_trusted = target.get("author_association") in check_in_status.FIX_CLAIM_TRUSTED_ASSOCIATIONS
	target_markers = _outside_fenced_examples(str(target.get("body") or "")) if target_trusted else ""
	# Condition 1: pipeline-filed.
	if issue.get("pull_request"):
		reasons.append(f"#{issue_number} is a pull request, not an issue")
	if issue.get("state") != "open":
		reasons.append(f"#{issue_number} is not open")
	if LABEL not in security_pass_skip._label_names(issue):
		reasons.append(f"#{issue_number} is not labelled {LABEL}")
	if not MARKER_RE.search(markers):
		reasons.append(f"#{issue_number} has no ai:permission-prompt signature marker")
	if FILED_BY_LINE not in markers:
		reasons.append(f"#{issue_number} has no '{FILED_BY_LINE}' line")
	author = security_pass_skip._login(issue.get("user"))
	if not login:
		reasons.append("the authenticated account is unknown")
	elif author != login:
		reasons.append(f"#{issue_number} author {author or 'unknown'} is not the session account {login}")
	if events is None:
		reasons.append(f"#{issue_number} events could not be read")
	elif len(events) >= security_pass_skip.EVENTS_PAGE_SIZE:
		reasons.append(f"#{issue_number} has {security_pass_skip.EVENTS_PAGE_SIZE}+ events; label history not verifiable in one page")
	else:
		problem = security_pass_skip._label_applied_at_creation(issue, events, LABEL)
		if problem:
			reasons.append(f"#{issue_number}: {problem}")
	# Condition 2: the target and its fix.
	target_closed_completed = target.get("state") == "closed" and target.get("state_reason") == "completed"
	if target_number == issue_number:
		reasons.append("the target is the issue itself")
	if target.get("pull_request"):
		reasons.append(f"#{target_number} is a pull request, not an issue")
	elif target.get("state") != "open" and not target_closed_completed:
		reasons.append(f"#{target_number} is closed as {target.get('state_reason') or 'unknown'}, not completed")
	if not target_trusted:
		reasons.append(f"#{target_number} author {_untrusted_author(target)} is not an owner, member, or collaborator")
	fix_number = fix_pr.get("number")
	merged = bool(fix_pr.get("merged_at"))
	# A fork PR, or one by an account without write access, is no verified fix,
	# and only such a PR's title, body, and branch name bind it to the target.
	head_repo = _repo_full_name(fix_pr.get("head"))
	base_repo_name = _repo_full_name(fix_pr.get("base"))
	if not head_repo or not base_repo_name or head_repo.casefold() != base_repo_name.casefold():
		reasons.append(f"PR #{fix_number} is not a same-repository PR (head {head_repo or 'unknown'})")
	if fix_pr.get("author_association") not in check_in_status.FIX_CLAIM_TRUSTED_ASSOCIATIONS:
		reasons.append(f"PR #{fix_number} author {_untrusted_author(fix_pr)} is not an owner, member, or collaborator")
	if not _references_issue(fix_pr, target_number):
		reasons.append(f"PR #{fix_number} does not reference #{target_number} (head branch issue-{target_number}-… or #{target_number} in its title or body)")
	if target_closed_completed:
		base = fix_pr.get("base") if isinstance(fix_pr.get("base"), dict) else {}
		base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
		default_branch = base_repo.get("default_branch")
		if not merged:
			reasons.append(f"#{target_number} is closed but PR #{fix_number} is not merged")
		elif not default_branch or base.get("ref") != default_branch:
			reasons.append(f"PR #{fix_number} merged into {base.get('ref') or 'unknown'}, not the default branch {default_branch or 'unknown'}")
	elif fix_pr.get("state") != "open" and not merged:
		reasons.append(f"PR #{fix_number} is closed without merging")
	return {
		"eligible": not reasons,
		"reasons": reasons,
		"issue": issue_number,
		"target": target_number,
		"fix_pr": fix_number,
		"fix_pr_state": "merged" if merged else fix_pr.get("state"),
		"signature": _first_match(MARKER_RE, markers, 1),
		"class": _first_match(CLASS_MARKER_RE, markers, 1),
		"target_class": _first_match(CLASS_MARKER_RE, target_markers, 1),
		"occurrences": _first_match(OCCURRENCES_RE, markers),
		"target_occurrences": _first_match(OCCURRENCES_RE, target_markers),
	}


def duplicate_check(repo: str, issue_number: int, target_number: int, fix_pr_number: int) -> tuple[int, dict]:
	"""Read what `decide_duplicate_close` needs (at most five GETs) and decide; exit 2 on a read failure."""
	if target_number == issue_number:
		return 0, {"eligible": False, "reasons": ["the target is the issue itself"], "issue": issue_number, "target": target_number, "fix_pr": fix_pr_number}
	if security_pass_skip is None:
		return 2, {"eligible": False, "reasons": [_SKIP_CHECK_ERROR], "issue": issue_number, "target": target_number, "fix_pr": fix_pr_number}
	try:
		login = str(check_in_status.gh_api("user").get("login") or "")
		issue = check_in_status.gh_api(f"repos/{repo}/issues/{issue_number}")
		events = check_in_status._gh_api_json(f"repos/{repo}/issues/{issue_number}/events?per_page={security_pass_skip.EVENTS_PAGE_SIZE}")
		target = check_in_status.gh_api(f"repos/{repo}/issues/{target_number}")
		fix_pr = check_in_status.gh_api(f"repos/{repo}/pulls/{fix_pr_number}")
	except check_in_status.ReadError as exc:
		return 2, {"eligible": False, "reasons": [str(exc)], "issue": issue_number, "target": target_number, "fix_pr": fix_pr_number}
	return 0, decide_duplicate_close(issue, events if isinstance(events, list) else None, target, fix_pr, login, issue_number, target_number)


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	for name in ("report", "file"):
		command = sub.add_parser(name)
		command.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
		if name == "file":
			command.add_argument("--session-label", default="unknown")
			command.add_argument("--dry-run", action="store_true")
	duplicate = sub.add_parser("duplicate-check")
	duplicate.add_argument("--repo", required=True)
	duplicate.add_argument("--issue", type=int, required=True)
	duplicate.add_argument("--target", type=int, required=True)
	duplicate.add_argument("--fix-pr", type=int, required=True)
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	if args.command == "duplicate-check":
		if not DUPLICATE_REPO_RE.match(args.repo) or min(args.issue, args.target, args.fix_pr) < 1:
			print(json.dumps({"eligible": False, "reasons": ["--repo must be OWNER/REPO and --issue, --target, --fix-pr positive numbers"]}))
			return 1
		code, verdict = duplicate_check(args.repo, args.issue, args.target, args.fix_pr)
		print(json.dumps(verdict))
		return code
	log_dir = Path(args.log_dir)
	if args.command == "report":
		print(json.dumps(report(log_dir)))
		return 0
	if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", args.session_label):
		print(json.dumps({"error": "--session-label must be 1-120 letters, digits, '_' or '-'"}))
		return 1
	code, summary = file_patterns(log_dir, args.session_label, args.dry_run)
	print(json.dumps(summary))
	return code


if __name__ == "__main__":
	sys.exit(main())
