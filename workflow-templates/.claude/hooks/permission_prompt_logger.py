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

Each line holds `ts`, `event` (`PermissionRequest` or `PermissionDenied`),
`session_id`, `tool_name`, `tool_input` (string values longer than
MAX_VALUE_CHARS are truncated), `reason` (from `reason` or
`decision_reason`, whichever the payload carries), `permission_mode`, and
`cwd`.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# The `.claude/settings.json` events this hook is wired under. Kept here so
# the tests can assert the wiring and the hook agree.
SETTINGS_EVENTS = ("PermissionRequest", "PermissionDenied")

LOG_DIR_PARTS = (".claude", "permission-prompts")
MAX_VALUE_CHARS = 8000
_SESSION_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]")


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


def append_record(record: dict, directory: Path) -> Path:
	directory.mkdir(parents=True, exist_ok=True)
	path = directory / f"{_SESSION_FILE_RE.sub('_', record['session_id'])[:120]}.jsonl"
	with path.open("a", encoding="utf-8") as handle:
		handle.write(json.dumps(record, ensure_ascii=False) + "\n")
	return path


def main() -> int:
	try:
		raw = sys.stdin.read()
		payload = json.loads(raw) if raw.strip() else {}
		if isinstance(payload, dict):
			record = build_record(payload, datetime.now(timezone.utc))
			if record is not None:
				append_record(record, log_dir())
	except Exception:  # noqa: BLE001 - logging must never affect the permission flow
		pass
	return 0


if __name__ == "__main__":
	sys.exit(main())
