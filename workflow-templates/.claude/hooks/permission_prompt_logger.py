#!/usr/bin/env python3
"""Record every permission prompt and Auto-mode denial a session hits.

Implements the logging half of CLAUDE.md §23.I (permission prompt reports).

Unattended `/implement-plan-claude` stage sessions stall when a tool call
needs a human: a `PermissionRequest` (the dialog is shown and nobody answers
it) or, in Auto mode, a `PermissionDenied` (the classifier blocked the call
and the session carries on without it). Until now the only way to find those
calls was a screenshot. This hook appends one JSON line per event to
`~/.claude/permission-prompts/<session id>.jsonl`, outside the repository, so
`.claude/scripts/permission_prompts.py` can list them in the stage report and
file them as `ai:permission-prompt` issues.

It never decides anything: it prints nothing, so the dialog (or the denial)
proceeds exactly as it would without the hook. It issues no API calls (§15),
reads no environment variables, and never fails the tool call: any error is
swallowed and the event is simply not logged.

After logging a `PermissionRequest` it starts one detached
`permission_prompts.py report-now` (issue #4755) and returns without waiting:
stdio goes to /dev/null and the child runs in its own session, so the prompt
is never delayed. It passes the SHA-256 of the line it wrote
(`--record-sha256`), so the child reports this prompt even when another one
is logged before it starts. The child decides whether the session is unattended and
reports the prompt to GitHub right away, because a session stuck on a prompt
never reaches the end-of-stage `permission_prompts.py file`. A spawn failure
is swallowed like any other error. `PermissionDenied` starts nothing.

Each line holds `ts`, `event` (`PermissionRequest` or `PermissionDenied`),
`session_id`, `tool_name`, `tool_input` (string values longer than
MAX_VALUE_CHARS are truncated), `reason` (from `reason` or
`decision_reason`, whichever the payload carries), `permission_mode`, and
`cwd`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# The `.claude/settings.json` events this hook is wired under. Kept here so
# the tests can assert the wiring and the hook agree.
SETTINGS_EVENTS = ("PermissionRequest", "PermissionDenied")

LOG_DIR_PARTS = (".claude", "permission-prompts")
MAX_VALUE_CHARS = 8000
_SESSION_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]")
# The helper `spawn_reporter` starts, resolved from this file so it works from any cwd.
REPORTER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "permission_prompts.py"


def log_dir() -> Path:
	return Path.home().joinpath(*LOG_DIR_PARTS)


def _truncate(value: object) -> object:
	if isinstance(value, str) and len(value) > MAX_VALUE_CHARS:
		return value[:MAX_VALUE_CHARS] + f"… [truncated {len(value) - MAX_VALUE_CHARS} chars]"
	if isinstance(value, dict):
		return {key: _truncate(item) for key, item in value.items()}
	if isinstance(value, list):
		return [_truncate(item) for item in value]
	return value


def build_record(payload: dict, now: datetime) -> dict | None:
	"""Return the log line for one hook payload, or None when it is not ours."""
	event = payload.get("hook_event_name")
	if event not in SETTINGS_EVENTS:
		return None
	reason = payload.get("reason")
	if not isinstance(reason, str) or not reason:
		reason = payload.get("decision_reason")
	return {
		"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
		"event": event,
		"session_id": str(payload.get("session_id") or "unknown"),
		"tool_name": str(payload.get("tool_name") or ""),
		"tool_input": _truncate(payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}),
		"reason": reason if isinstance(reason, str) else "",
		"permission_mode": str(payload.get("permission_mode") or ""),
		"cwd": str(payload.get("cwd") or ""),
	}


def serialize_record(record: dict) -> str:
	"""The log line for a record, without its newline; `record_digest` hashes exactly this text."""
	return json.dumps(record, ensure_ascii=False)


def record_digest(record: dict) -> str:
	"""SHA-256 hex of the record's log line, which `permission_prompts.py report-now --record-sha256` looks up."""
	return hashlib.sha256(serialize_record(record).encode("utf-8")).hexdigest()


def append_record(record: dict, directory: Path) -> Path:
	directory.mkdir(parents=True, exist_ok=True)
	path = directory / f"{_SESSION_FILE_RE.sub('_', record['session_id'])[:120]}.jsonl"
	with path.open("a", encoding="utf-8") as handle:
		handle.write(serialize_record(record) + "\n")
	return path


def spawn_reporter(log_path: Path, cwd: str, digest: str = "") -> None:
	"""Start `permission_prompts.py report-now` detached and return at once; never waits on the child."""
	if not REPORTER_PATH.is_file():
		return
	args = [sys.executable, "-B", str(REPORTER_PATH), "report-now", "--log-file", str(log_path), "--cwd", cwd]
	if digest:
		args += ["--record-sha256", digest]
	subprocess.Popen(
		args,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		start_new_session=True,
		close_fds=True,
	)


def main() -> int:
	try:
		raw = sys.stdin.read()
		payload = json.loads(raw) if raw.strip() else {}
		if isinstance(payload, dict):
			record = build_record(payload, datetime.now(timezone.utc))
			if record is not None:
				path = append_record(record, log_dir())
				if record["event"] == "PermissionRequest":
					spawn_reporter(path, record["cwd"], record_digest(record))
	except Exception:  # noqa: BLE001 - logging must never affect the permission flow
		pass
	return 0


if __name__ == "__main__":
	sys.exit(main())
