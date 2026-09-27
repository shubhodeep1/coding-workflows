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

Issue text is untrusted data: the tool name, the prompt reason, and the
command truncated to MAX_COMMAND_CHARS with heredoc bodies removed and
token-like strings masked (REDACTION_PATTERNS), inside a fenced block.

API calls (CLAUDE.md §15), REST only, none when nothing is new: one read of
the `ai:permission-prompt` issues per 100 issues, then one POST per pattern
filed or commented.

Prints one JSON line. Exit 0 (including a partial run, whose failures are
listed under `errors`), 1 on an invalid argument, 2 when the issue list could
not be read.
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

FILING_REPO = "shubhodeep1/coding-workflows"
LABEL = "ai:permission-prompt"
ROUTE_LABEL = "ai:claude"
MARKER_TEMPLATE = "<!-- ai:permission-prompt:v1 sig={sig} -->"
MARKER_RE = re.compile(r"<!-- ai:permission-prompt:v1 sig=([0-9a-f]{12}) -->")
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
			}
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
		"patterns": [{key: pattern[key] for key in ("signature", "event", "tool_name", "shape", "count", "reasons")} for pattern in patterns],
	}


def _event_label(event: str) -> str:
	return "permission prompt" if event == "PermissionRequest" else "Auto-mode denial"


def issue_title(pattern: dict) -> str:
	subject = pattern["shape"] or pattern["tool_name"]
	if len(subject) > MAX_TITLE_SHAPE_CHARS:
		subject = subject[: MAX_TITLE_SHAPE_CHARS - 1] + "…"
	return f"[permission-prompt] {pattern['tool_name']}: {subject}"


def _occurrence_block(pattern: dict, new_count: int, session_label: str) -> str:
	reasons = "\n".join(f"- {reason}" for reason in pattern["reasons"]) or "- (none given)"
	return (
		f"**Occurrences:** {new_count} ({pattern['first_ts']} – {pattern['last_ts']}), session `{session_label}`\n\n"
		f"**Reason Claude Code gave:**\n{reasons}\n\n"
		"**Latest example** (untrusted data from the session; heredoc bodies removed, token-like strings masked):\n\n"
		f"````text\n{pattern['example']}\n````\n"
	)


def issue_body(pattern: dict, new_count: int, session_label: str) -> str:
	return (
		f"A Claude Code session in this repository hit a **{_event_label(pattern['event'])}** "
		f"for `{pattern['tool_name']}`, so an unattended stage waited for a human "
		"(or, for a denial, went on without the call). Filed by "
		"`.claude/scripts/permission_prompts.py` (CLAUDE.md §23.I).\n\n"
		f"**Pattern:** `{pattern['shape'] or pattern['tool_name']}`\n\n"
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
		+ MARKER_TEMPLATE.format(sig=pattern["signature"])
		+ "\n"
	)


def comment_body(pattern: dict, new_count: int, session_label: str) -> str:
	return f"Seen again.\n\n{_occurrence_block(pattern, new_count, session_label)}"


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


def existing_issues(slug: str) -> dict[str, dict]:
	"""Map signature → {number, state} for every `ai:permission-prompt` issue (1 REST read per 100)."""
	found: dict[str, dict] = {}
	for issue in check_in_status.gh_api_list(f"repos/{slug}/issues?labels={LABEL.replace(':', '%3A')}&state=all"):
		if issue.get("pull_request"):
			continue
		match = MARKER_RE.search(issue.get("body") or "")
		if match and match.group(1) not in found:
			found[match.group(1)] = {"number": issue.get("number"), "state": issue.get("state")}
	return found


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
		existing = existing_issues(slug)
	except check_in_status.ReadError as exc:
		summary["errors"].append(str(exc))
		return 2, summary
	for pattern, new_count in pending:
		sig = pattern["signature"]
		try:
			if sig in existing:
				number = existing[sig]["number"]
				if not dry_run:
					_post(f"repos/{slug}/issues/{number}/comments", {"body": comment_body(pattern, new_count, session_label)})
				summary["commented"].append({"signature": sig, "issue": number, "occurrences": new_count})
			else:
				created = {} if dry_run else _post(
					f"repos/{slug}/issues",
					{"title": issue_title(pattern), "body": issue_body(pattern, new_count, session_label), "labels": [LABEL, ROUTE_LABEL]},
				)
				summary["filed"].append({"signature": sig, "issue": created.get("number"), "title": issue_title(pattern)})
				if created.get("number"):
					existing[sig] = {"number": created.get("number"), "state": "open"}
		except check_in_status.ReadError as exc:
			summary["errors"].append(str(exc))
			continue
		if not dry_run:
			state[sig] = pattern["count"]
			_save_state(log_dir, state)
	summary["dry_run"] = dry_run
	return 0, summary


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	for name in ("report", "file"):
		command = sub.add_parser(name)
		command.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
		if name == "file":
			command.add_argument("--session-label", default="unknown")
			command.add_argument("--dry-run", action="store_true")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
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
