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
  permission_prompts.py report-now --log-file FILE [--cwd DIR] [--record-sha256 HEX]
  permission_prompts.py lookup --session ID [--repo OWNER/REPO]
  permission_prompts.py session-meta --title TITLE [--log-dir DIR]

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
in the same session files only what is new. `file` also skips the
occurrences `report-now` already delivered to this repository, so a prompt
is reported once per session: a signature whose every occurrence came from
a session that reported it here is listed under `already_reported`. A report
that went to another repository (a consumer's PR or issue), or that another
session made, never suppresses the rest (issue #5125); each report records
its `repo` and the logged session (`log_session`) it came from.

`report-now` is the immediate report (issue #4755). The logger hook starts it
detached on every `PermissionRequest`, so it prints nothing, always exits 0,
and swallows every error: the prompt is never blocked, delayed, or changed.
The hook passes `--record-sha256`, the SHA-256 of the log line it just wrote,
so the report is about that prompt even when a later prompt was logged
before the child started (without it, the last line is used). Setting
REPORT_NOW_SWITCH_ENV (`CLAUDE_PERMISSION_PROMPT_REPORT`) to `off` turns
`report-now` off; unset (the default) it is on.
It reports only in an unattended session, meaning a cloud session
(CLAUDE_CODE_REMOTE_SESSION_ID set) whose prompt came in `auto` or
`bypassPermissions` mode. Each signature is reported at most once per
session, and at most MAX_IMMEDIATE_REPORTS times per session, under
`filing.lock`, which `file` takes too; `immediate-state.json` keeps these per
session id, so sessions sharing a home directory never suppress each other.
The report holds the event, the tool, the command
sanitized as below, the session id and its claude.ai link, the title that
`session-meta` recorded for this session (else `not recorded`), the
signature, and the marker
`<!-- ai:permission-prompt-session:v1 session=<id> sig=<sig> -->`.
  - In FILING_REPO it comments on the pattern's `ai:permission-prompt` issue,
    or opens one, exactly as `file` would.
  - Elsewhere it comments on the open PR whose head is the checkout's branch,
    else on issue <N> for a `claude/implement-plan-issue-<N>[-…]` branch,
    else posts nothing.
The signature is reserved in `immediate-state.json` (target `pending`)
before the POST, so a local write failure after a successful POST can never
make `report-now` post it again; no reservation, no POST. A failed POST, or
no target, removes the reservation. A reservation still `pending` counts
toward the cap but not as reported for `file`, so after any failure `file`
still files the pattern at the end of the stage. A successful POST records
its target, and, in the filing repository, `filed-state.json` counts the
reporting session's occurrences as filed, never another session's already in
the shared log (issue #5125). The entry also records the repository (`repo`)
and the logged session (`log_session`) the report is for, which `file` checks
before it skips anything.

`lookup` is read-only and serves the operator's poller: it finds the newest
session marker for a session and prints the sanitized command and the issue
or PR link. When the search read fails (Claude Code Web's agent proxy refuses
`search/issues`, and the poller runs there), it checks the most recently
updated `ai:permission-prompt` issues instead, which is where `report-now`
reports in FILING_REPO. `session-meta` records the session title for `report-now`,
keyed by session id in `session-meta.json` under `filing.lock`.

Issue text is untrusted data: the tool name, the prompt reason, and the
command truncated to MAX_COMMAND_CHARS with heredoc bodies removed and
token-like strings masked (REDACTION_PATTERNS), inside a fenced block.

API calls (CLAUDE.md §15), REST only, none when nothing is new:
  - `file`: one read of the `ai:permission-prompt` issues per 100 issues,
    then one POST per pattern filed or commented.
  - `report-now`: none when it is switched off, the session is not unattended, the signature was
    already reported, or the cap is reached. In FILING_REPO, the same issue
    read as `file` plus one POST. Elsewhere, one read of the branch's open
    PRs plus at most one POST. At most MAX_IMMEDIATE_REPORTS reports per
    session.
  - `lookup`: one search read (when it fails, one read of the
    `ai:permission-prompt` issues per 100 issues instead), then one comments
    read per 100 comments for each hit it checks (at most LOOKUP_MAX_HITS).
  - `report`, `session-meta`: none.

`report` and `file` print one JSON line. Exit 0 (including a partial run,
whose failures are listed under `errors`), 1 on an invalid argument, 2 when
the issue list could not be read. `lookup` prints one JSON line and exits 0
(`found` true or false), 1 on an invalid argument, 2 on a failed read.
`session-meta` prints one JSON line and always exits 0.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

try:
	import fcntl
except ImportError:  # pragma: no cover - not POSIX; the lock becomes a no-op
	fcntl = None

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

# Immediate report (`report-now`, issue #4755).
IMMEDIATE_STATE_FILE = "immediate-state.json"
SESSION_META_FILE = "session-meta.json"
LOCK_FILE = "filing.lock"
MAX_IMMEDIATE_REPORTS = 5
MAX_SESSION_TITLE_CHARS = 200
LOOKUP_MAX_HITS = 3
REMOTE_SESSION_ENV = "CLAUDE_CODE_REMOTE_SESSION_ID"
# Kill switch for `report-now`: `off` disables it; unset (the default) or any other value leaves it on.
REPORT_NOW_SWITCH_ENV = "CLAUDE_PERMISSION_PROMPT_REPORT"
# Target of a signature reserved in immediate-state.json while its POST is in flight.
PENDING_TARGET = "pending"
UNATTENDED_MODES = frozenset({"auto", "bypassPermissions"})
SESSION_URL_TEMPLATE = "https://claude.ai/code/{session}"
IMMEDIATE_HEADING = "**Immediate report:** an unattended session is waiting on this permission prompt now."
SESSION_MARKER_TEMPLATE = "<!-- ai:permission-prompt-session:v1 session={session} sig={sig} -->"
SESSION_MARKER_RE = re.compile(r"<!-- ai:permission-prompt-session:v1 session=(session_[A-Za-z0-9_-]{1,120}) sig=([0-9a-f]{12}) -->")
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,120}")
_ISSUE_BRANCH_RE = re.compile(r"^claude/implement-plan-issue-([0-9]+)(?:-|$)")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

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
			# Split on "\n" only: the hook writes JSON with ensure_ascii=False, so a
			# command can hold U+2028 or U+0085, which splitlines() would also split on.
			lines = path.read_text(encoding="utf-8").split("\n")
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
				"sessions": {},
				"reasons": [],
				"first_ts": record.get("ts"),
				"last_ts": record.get("ts"),
				"example": "",
			}
		pattern["count"] += 1
		# Occurrences per logged session, so `file` can skip only a session's own reported ones (issue #5125).
		log_session = str(record.get("session_id") or "")
		pattern["sessions"][log_session] = pattern["sessions"].get(log_session, 0) + 1
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


def _git_output(args: list[str], cwd: str | None) -> str:
	"""Stripped stdout of one `git` command in `cwd` (the current directory when empty), or "" on any failure."""
	try:
		proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5, cwd=cwd or None)
	except (OSError, subprocess.SubprocessError):
		return ""
	return proc.stdout.strip() if proc.returncode == 0 else ""


def local_repo_slug(cwd: str | None = None) -> str:
	return extract_repo_slug(_git_output(["config", "--get", "remote.origin.url"], cwd))


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


@contextlib.contextmanager
def _filing_lock(log_dir: Path):
	"""Hold an exclusive lock on `<log dir>/filing.lock` around every state read-modify-write."""
	log_dir.mkdir(parents=True, exist_ok=True)
	with (log_dir / LOCK_FILE).open("a", encoding="utf-8") as handle:
		if fcntl is not None:
			fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
		try:
			yield
		finally:
			if fcntl is not None:
				fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_immediate_state(log_dir: Path) -> dict:
	"""`{"sessions": {label: {"reports": {sig: {...}}, "count": n}}}` from `immediate-state.json`; empty when missing or invalid."""
	try:
		state = json.loads((log_dir / IMMEDIATE_STATE_FILE).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		state = {}
	sessions = state.get("sessions") if isinstance(state, dict) else None
	loaded: dict[str, dict] = {}
	for label, bucket in (sessions.items() if isinstance(sessions, dict) else ()):
		if not isinstance(label, str) or not isinstance(bucket, dict):
			continue
		reports = bucket.get("reports")
		reports = {key: value for key, value in reports.items() if isinstance(key, str) and isinstance(value, dict)} if isinstance(reports, dict) else {}
		count = bucket.get("count")
		loaded[label] = {"reports": reports, "count": count if isinstance(count, int) and count >= len(reports) else len(reports)}
	return {"sessions": loaded}


def _session_reports(immediate: dict, session_label: str) -> dict:
	"""This session's `{"reports": {...}, "count": n}` bucket in a loaded immediate state, created empty when missing."""
	return immediate["sessions"].setdefault(session_label, {"reports": {}, "count": 0})


def _save_immediate_state(log_dir: Path, state: dict) -> None:
	log_dir.mkdir(parents=True, exist_ok=True)
	(log_dir / IMMEDIATE_STATE_FILE).write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def session_label_from_env() -> str:
	"""`session_<id>` for this cloud session (CLAUDE_CODE_REMOTE_SESSION_ID, `cse_` prefix stripped), or ""."""
	raw = os.environ.get(REMOTE_SESSION_ENV, "").strip()
	return normalize_session_label(raw)


def normalize_session_label(raw: str) -> str:
	"""`session_<id>` from `session_<id>`, `cse_<id>`, or a bare id; "" when it is not a safe id."""
	raw = raw.strip()
	for prefix in ("session_", "cse_"):
		if raw.startswith(prefix):
			raw = raw[len(prefix) :]
			break
	return f"session_{raw}" if _SESSION_ID_RE.fullmatch(raw) else ""


def is_unattended(record: dict, session_label: str) -> bool:
	"""True for a `PermissionRequest` in a cloud session running in `auto` or `bypassPermissions` mode."""
	return (
		bool(session_label)
		and record.get("event") == "PermissionRequest"
		and str(record.get("permission_mode") or "") in UNATTENDED_MODES
	)


def _inline_code(text: str) -> str:
	"""One line of untrusted text, safe inside a Markdown code span."""
	return " ".join(text.replace("`", "'").split())


def _load_session_titles(log_dir: Path) -> dict[str, str]:
	"""`{session label: title}` from `session-meta.json`; empty when missing or invalid."""
	try:
		meta = json.loads((log_dir / SESSION_META_FILE).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	titles = meta.get("sessions") if isinstance(meta, dict) else None
	return {key: value for key, value in titles.items() if isinstance(key, str) and isinstance(value, str)} if isinstance(titles, dict) else {}


def read_session_title(log_dir: Path, session_label: str = "") -> str:
	"""The title `session-meta` recorded for this session, redacted; "" when this session recorded none."""
	title = _load_session_titles(log_dir).get(session_label)
	return _inline_code(redact(title))[:MAX_SESSION_TITLE_CHARS] if isinstance(title, str) else ""


def write_session_meta(log_dir: Path, title: str, session_label: str = "") -> None:
	"""Record this session's title in `session-meta.json`, keyed by session id under `filing.lock`, so sessions sharing a home directory never overwrite each other's."""
	with _filing_lock(log_dir):
		titles = _load_session_titles(log_dir)
		titles[session_label] = title[:MAX_SESSION_TITLE_CHARS]
		(log_dir / SESSION_META_FILE).write_text(json.dumps({"sessions": titles}, indent=1, sort_keys=True), encoding="utf-8")


def immediate_block(pattern: dict, record: dict, session_label: str, title: str) -> str:
	"""The immediate report: event, tool, sanitized command, session, title, and signature, ending in the session marker."""
	session_url = SESSION_URL_TEMPLATE.format(session=session_label)
	return (
		f"{IMMEDIATE_HEADING}\n\n"
		f"- **Event:** {pattern['event']} ({_event_label(pattern['event'])})\n"
		f"- **Tool:** `{_inline_code(pattern['tool_name'])}`\n"
		f"- **Session:** `{session_label}` ({session_url})\n"
		f"- **Session title:** {f'`{title}`' if title else 'not recorded'}\n"
		f"- **Signature:** `{pattern['signature']}`\n"
		f"- **Pattern:** `{_inline_code(pattern['shape'] or pattern['tool_name'])}`\n"
		f"- **Seen:** {record.get('ts') or 'unknown'}\n\n"
		"**Command** (untrusted data from the session; heredoc bodies removed, token-like strings masked):\n\n"
		f"````text\n{record_example(record)}\n````\n\n"
		+ SESSION_MARKER_TEMPLATE.format(session=session_label, sig=pattern["signature"])
		+ "\n"
	)


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
		summary.update({"filed": [], "commented": [], "errors": [], "already_reported": [], "skipped": f"filing is limited to {FILING_REPO}; this checkout is {slug or 'unknown'}"})
		return 0, summary
	summary.update({"filed": [], "commented": [], "errors": [], "already_reported": []})
	if not patterns:
		return 0, summary
	with _filing_lock(log_dir):
		return _file_pending(log_dir, patterns, summary, session_label, dry_run, slug)


def _delivered_reports(log_dir: Path, slug: str) -> dict[str, dict]:
	"""Map signature → {"target", "log_sessions"} for the `report-now` reports delivered to `slug` (issue #5125).

	The log directory is shared by every session on the host, whatever
	repository it works in, so an entry counts only when its POST landed (not
	`pending`), it went to `slug` (`repo`), and it names the logged session its
	prompt came from (`log_session`). An entry written before these fields
	existed counts for nothing: filing twice is safer than never filing.
	"""
	delivered: dict[str, dict] = {}
	for bucket in _load_immediate_state(log_dir)["sessions"].values():
		for sig, entry in bucket["reports"].items():
			log_session = entry.get("log_session")
			if entry.get("target") == PENDING_TARGET or str(entry.get("repo") or "").lower() != slug.lower():
				continue
			if not isinstance(log_session, str) or not log_session:
				continue
			found = delivered.setdefault(sig, {"target": entry.get("target"), "log_sessions": set()})
			found["log_sessions"].add(log_session)
	return delivered


def _file_pending(log_dir: Path, patterns: list[dict], summary: dict, session_label: str, dry_run: bool, slug: str) -> tuple[int, dict]:
	"""The filing half of `file_patterns`, run under `filing.lock`."""
	state = _load_state(log_dir)
	delivered = _delivered_reports(log_dir, slug)
	pending = []
	for pattern in patterns:
		sig = pattern["signature"]
		new_count = pattern["count"] - state.get(sig, 0)
		if sig in delivered:
			# `report-now` already delivered this signature here for some sessions (issue #4755): their
			# occurrences are covered; every other session's are still filed (issue #5125).
			covered_sessions = delivered[sig]["log_sessions"]
			uncovered = sum(count for log_session, count in pattern["sessions"].items() if log_session not in covered_sessions)
			if uncovered <= 0:
				summary["already_reported"].append({"signature": sig, "target": delivered[sig]["target"]})
				continue
			new_count = min(new_count, uncovered)
		if new_count > 0:
			pending.append((pattern, new_count))
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


def _last_record(log_file: Path) -> dict | None:
	"""The last JSON line of one session log (the prompt the hook just appended), or None."""
	try:
		lines = log_file.read_text(encoding="utf-8").split("\n")
	except OSError:
		return None
	for line in reversed(lines):
		if not line.strip():
			continue
		try:
			record = json.loads(line)
		except ValueError:
			return None
		return record if isinstance(record, dict) else None
	return None


def _record_by_digest(log_file: Path, record_digest: str) -> dict | None:
	"""The newest log line whose SHA-256 (of the line without its newline) is `record_digest`, or None."""
	try:
		lines = log_file.read_text(encoding="utf-8").split("\n")
	except OSError:
		return None
	for line in reversed(lines):
		if hashlib.sha256(line.encode("utf-8")).hexdigest() != record_digest:
			continue
		try:
			record = json.loads(line)
		except ValueError:
			return None
		return record if isinstance(record, dict) else None
	return None


def _report_to_filing_repo(slug: str, pattern: dict, block: str, session_label: str, log_dir: Path) -> str:
	"""Comment on the pattern's `ai:permission-prompt` issue, or open it, with the immediate block appended."""
	existing = existing_issues(slug)
	new_count = max(pattern["count"] - _load_state(log_dir).get(pattern["signature"], 0), 1)
	if pattern["signature"] in existing:
		number = existing[pattern["signature"]]["number"]
		_post(f"repos/{slug}/issues/{number}/comments", {"body": comment_body(pattern, new_count, session_label) + "\n" + block})
		return f"issue #{number}"
	created = _post(
		f"repos/{slug}/issues",
		{"title": issue_title(pattern), "body": issue_body(pattern, new_count, session_label) + "\n" + block, "labels": [LABEL, ROUTE_LABEL]},
	)
	return f"issue #{created['number']}" if isinstance(created.get("number"), int) else "issue #?"


def _report_to_own_thread(slug: str, cwd: str | None, block: str) -> str:
	"""Outside FILING_REPO: comment on the branch's open PR, else on a `claude/implement-plan-issue-<N>[-…]` issue; "" when neither exists."""
	branch = _git_output(["branch", "--show-current"], cwd)
	if not branch:
		return ""
	head = urllib.parse.quote(f"{slug.split('/', 1)[0]}:{branch}", safe="")
	pulls = check_in_status.gh_api_list(f"repos/{slug}/pulls?state=open&head={head}")
	number = next((pull["number"] for pull in pulls if isinstance(pull.get("number"), int)), None)
	kind = "PR"
	if number is None:
		match = _ISSUE_BRANCH_RE.match(branch)
		if not match:
			return ""
		number, kind = int(match.group(1)), "issue"
	body = (
		"A permission prompt is blocking the unattended Claude Code session working on this "
		f"{'pull request' if kind == 'PR' else 'issue'}. Reported by `.claude/scripts/permission_prompts.py` "
		"(CLAUDE.md §23.I).\n\n" + block
	)
	_post(f"repos/{slug}/issues/{number}/comments", {"body": body})
	return f"{kind} #{number}"


def report_now(log_file: Path, cwd: str | None, session_label: str | None = None, now: datetime | None = None, record_digest: str | None = None) -> str:
	"""Report the prompt the hook just logged, at most once per signature per session; see the module docstring.

	Never raises: the hook runs it detached and nobody reads its result. The
	returned outcome string exists for the tests. `record_digest` is the
	SHA-256 of the logged line to report; without it the last line is used.
	"""
	try:
		if os.environ.get(REPORT_NOW_SWITCH_ENV, "").strip().lower() == "off":
			return "skipped: switched off"
		return _report_now(log_file, cwd, session_label_from_env() if session_label is None else session_label, now or datetime.now(timezone.utc), record_digest)
	except Exception as exc:  # noqa: BLE001 - the immediate report must fail open
		return f"error: {exc}"


def _report_now(log_file: Path, cwd: str | None, session_label: str, now: datetime, record_digest: str | None = None) -> str:
	if record_digest:
		if not _SHA256_RE.fullmatch(record_digest):
			return "skipped: invalid record digest"
		record = _record_by_digest(log_file, record_digest)
	else:
		record = _last_record(log_file)
	if record is None or not is_unattended(record, session_label):
		return "skipped: not an unattended permission prompt"
	log_dir = log_file.parent
	sig = signature(str(record.get("event")), str(record.get("tool_name") or ""), record_shape(record))
	with _filing_lock(log_dir):
		immediate = _load_immediate_state(log_dir)
		bucket = _session_reports(immediate, session_label)
		if sig in bucket["reports"]:
			return "skipped: already reported"
		if bucket["count"] >= MAX_IMMEDIATE_REPORTS:
			return "skipped: cap reached"
		pattern = next((item for item in group_patterns(load_records(log_dir)) if item["signature"] == sig), None)
		if pattern is None:
			return "skipped: pattern not found"
		slug = local_repo_slug(cwd)
		if not slug:
			return "skipped: unknown repository"
		block = immediate_block(pattern, record, session_label, read_session_title(log_dir, session_label))
		filing_repo = slug.lower() == FILING_REPO
		# Reserve before the POST: when this write fails nothing is posted, and a
		# write failure after the POST can no longer lead to a second report.
		# `repo` and `log_session` let `file` skip only what reached its own repository (issue #5125).
		entry = {
			"target": PENDING_TARGET,
			"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
			"session": session_label,
			"repo": slug.lower(),
			"log_session": str(record.get("session_id") or ""),
		}
		bucket["reports"][sig] = entry
		bucket["count"] += 1
		_save_immediate_state(log_dir, immediate)
		try:
			target = _report_to_filing_repo(slug, pattern, block, session_label, log_dir) if filing_repo else _report_to_own_thread(slug, cwd, block)
		except Exception:
			_release_reservation(log_dir, immediate, bucket, sig)
			raise
		if not target:
			_release_reservation(log_dir, immediate, bucket, sig)
			return "skipped: no target"
		entry["target"] = target
		_save_immediate_state(log_dir, immediate)
		if filing_repo:
			state = _load_state(log_dir)
			# Only this logged session's occurrences are covered: another session's, already in the
			# shared log, stay unfiled so `file` still files them (issue #5125).
			state[sig] = max(state.get(sig, 0), pattern["sessions"].get(entry["log_session"], 0))
			_save_state(log_dir, state)
		return f"reported: {target}"


def _release_reservation(log_dir: Path, immediate: dict, bucket: dict, sig: str) -> None:
	"""Undo a `pending` reservation whose POST failed or had no target; a failed write leaves it `pending`, which `file` ignores."""
	bucket["reports"].pop(sig, None)
	bucket["count"] = max(bucket["count"] - 1, 0)
	with contextlib.suppress(OSError):
		_save_immediate_state(log_dir, immediate)


_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def parse_immediate_block(text: str, session_label: str) -> dict | None:
	"""The fields of the newest immediate block in `text` for this session, or None."""
	markers = [match for match in SESSION_MARKER_RE.finditer(text) if match.group(1) == session_label]
	if not markers:
		return None
	marker = markers[-1]
	head = text[: marker.start()]
	start = head.rfind(IMMEDIATE_HEADING)
	if start < 0:
		return None
	section = head[start:]

	def field(name: str) -> str:
		match = re.search(rf"^- \*\*{re.escape(name)}:\*\* (.*)$", section, re.MULTILINE)
		return match.group(1).strip() if match else ""

	title = field("Session title")
	command = re.search(r"^````text\n(.*)\n````$", section, re.MULTILINE | re.DOTALL)
	return {
		"signature": marker.group(2),
		"event": field("Event").split(" (", 1)[0],
		"tool_name": field("Tool").strip("`"),
		"command": command.group(1) if command else "",
		"title": title[1:-1] if len(title) >= 2 and title.startswith("`") and title.endswith("`") else "",
	}


def _lookup_candidates(session_label: str, slug: str) -> list:
	"""The issues `lookup` checks: its search hits, or the newest-updated `ai:permission-prompt` issues when the search read fails.

	Claude Code Web's agent proxy refuses `search/issues` (HTTP 403, "sessions
	are bound to their configured repositories"), and the operator's poller runs
	there. The repository-scoped list of pattern issues, which `report-now`
	comments on or opens in FILING_REPO, still answers: 1 read per 100 labelled
	issues, the same read as `existing_issues`. Raises ReadError when both reads fail.
	"""
	query = urllib.parse.quote(f'repo:{slug} "{session_label}"', safe="")
	try:
		result = check_in_status.gh_api(f"search/issues?q={query}&sort=updated&order=desc&per_page={LOOKUP_MAX_HITS}")
	except check_in_status.ReadError:
		return check_in_status.gh_api_list(f"repos/{slug}/issues?labels={LABEL.replace(':', '%3A')}&state=all&sort=updated&direction=desc")[:LOOKUP_MAX_HITS]
	items = result.get("items")
	return items if isinstance(items, list) else []


def lookup(session_label: str, slug: str) -> dict:
	"""Find the newest immediate report for a session; see the module docstring. Raises ReadError on a failed read."""
	for item in _lookup_candidates(session_label, slug)[:LOOKUP_MAX_HITS]:
		if not isinstance(item, dict) or not isinstance(item.get("number"), int):
			continue
		issue_url = str(item.get("html_url") or "")
		if item.get("author_association") in _TRUSTED_ASSOCIATIONS:
			parsed = parse_immediate_block(str(item.get("body") or ""), session_label)
			if parsed:
				return {"found": True, "session": session_label, "issue_url": issue_url, "comment_url": "", **parsed}
		comments = check_in_status.gh_api_list(f"repos/{slug}/issues/{item['number']}/comments")
		for comment in reversed(comments):
			if comment.get("author_association") not in _TRUSTED_ASSOCIATIONS:
				continue
			parsed = parse_immediate_block(str(comment.get("body") or ""), session_label)
			if parsed:
				return {"found": True, "session": session_label, "issue_url": issue_url, "comment_url": str(comment.get("html_url") or ""), **parsed}
	return {"found": False, "session": session_label}


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	for name in ("report", "file"):
		command = sub.add_parser(name)
		command.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
		if name == "file":
			command.add_argument("--session-label", default="unknown")
			command.add_argument("--dry-run", action="store_true")
	report_now_command = sub.add_parser("report-now")
	report_now_command.add_argument("--log-file", required=True)
	report_now_command.add_argument("--cwd", default="")
	report_now_command.add_argument("--record-sha256", default="")
	lookup_command = sub.add_parser("lookup")
	lookup_command.add_argument("--session", required=True)
	lookup_command.add_argument("--repo", default="")
	meta_command = sub.add_parser("session-meta")
	meta_command.add_argument("--title", required=True)
	meta_command.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	if args.command == "report-now":
		report_now(Path(args.log_file), args.cwd or None, record_digest=args.record_sha256 or None)
		return 0
	if args.command == "session-meta":
		try:
			write_session_meta(Path(args.log_dir), args.title, session_label_from_env())
		except OSError as exc:
			print(json.dumps({"ok": False, "error": str(exc)}))
			return 0
		print(json.dumps({"ok": True}))
		return 0
	if args.command == "lookup":
		session_label = normalize_session_label(args.session)
		slug = args.repo or local_repo_slug()
		if not session_label or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9._-]+", slug):
			print(json.dumps({"error": "--session must be a session id and --repo (or the checkout) must name owner/repo"}))
			return 1
		try:
			print(json.dumps(lookup(session_label, slug)))
		except check_in_status.ReadError as exc:
			print(json.dumps({"found": False, "session": session_label, "error": str(exc)}))
			return 2
		return 0
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
