#!/usr/bin/env python3
"""Record which `.claude/settings.json` this session has loaded.

Implements the recording half of the Claude-asset sync's settings check
(issue #5259, `.claude/commands/implement-plan-claude.md`, "Claude-asset
sync" step 6 and "Settings restart").

Claude Code reads `.claude/settings.json` when a session starts and, through
its file watcher, reloads it when it changes on disk, running the
`ConfigChange` hook for each change it detects before applying it. The
watcher can miss a change (the hooks guide says to restart the session to
force a reload then), so after the asset sync merges a new `settings.json` a
session cannot tell whether the new hook wiring is active. This hook writes
what was loaded to `~/.claude/loaded-settings/<session id>.json`, outside the
repository, so `.claude/scripts/loaded_settings_check.py` can compare it with
the working tree:

- `SessionStart` with `source` `startup`, `resume`, or `fork`: a process that
  read the file at start. `clear` and `compact` keep the running process's
  settings, so they record nothing and the earlier record stands.
- `ConfigChange` with `source` `project_settings`: a change the watcher
  detected (the payload names the changed file in `file_path`). This must
  stay the only `ConfigChange` hook wired in `.claude/settings.json`: a hook
  that blocked a change would leave a record for a change that was never
  applied.

The record holds `ts`, `event`, `source`, `session_id`, `path`, and `sha256`
(the file's sha256, or `absent` when there is no file) and replaces the
previous one. The hook never decides anything: it prints nothing and exits 0,
so the session start or the config change proceeds exactly as without it. It
issues no API calls (§15), reads only `CLAUDE_PROJECT_DIR` from the
environment (falling back to the payload's `cwd`), and swallows every error;
a missing record makes the check report "not current", which fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


# The `.claude/settings.json` events this hook is wired under. Kept here so
# the tests can assert the wiring and the hook agree.
SETTINGS_EVENTS = ("SessionStart", "ConfigChange")
SESSION_START_SOURCES = ("startup", "resume", "fork")
CONFIG_CHANGE_SOURCE = "project_settings"

RECORD_DIR_PARTS = (".claude", "loaded-settings")
SETTINGS_RELATIVE_PATH = Path(".claude") / "settings.json"
ABSENT = "absent"
_SESSION_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def record_dir() -> Path:
	return Path.home().joinpath(*RECORD_DIR_PARTS)


def record_path(directory: Path, session_id: str) -> Path:
	return directory / f"{_SESSION_FILE_RE.sub('_', session_id)[:120]}.json"


def file_sha256(path: Path) -> str:
	"""The sha256 of `path`, or ABSENT when it is not a readable file."""
	try:
		return hashlib.sha256(path.read_bytes()).hexdigest()
	except OSError:
		return ABSENT


def settings_path(payload: dict, project_dir: str | None) -> Path | None:
	"""The settings file this event is about, or None when it is not ours."""
	event = payload.get("hook_event_name")
	if event == "SessionStart":
		if payload.get("source") not in SESSION_START_SOURCES:
			return None
		base = project_dir or payload.get("cwd")
		if not isinstance(base, str) or not base:
			return None
		return Path(base) / SETTINGS_RELATIVE_PATH
	if event == "ConfigChange":
		if payload.get("source") != CONFIG_CHANGE_SOURCE:
			return None
		changed = payload.get("file_path")
		if isinstance(changed, str) and changed:
			return Path(changed)
		base = project_dir or payload.get("cwd")
		if not isinstance(base, str) or not base:
			return None
		return Path(base) / SETTINGS_RELATIVE_PATH
	return None


def build_record(payload: dict, now: datetime, project_dir: str | None) -> dict | None:
	"""Return the record for one hook payload, or None when it is not ours."""
	session_id = payload.get("session_id")
	if not isinstance(session_id, str) or not session_id:
		return None
	path = settings_path(payload, project_dir)
	if path is None:
		return None
	event = payload["hook_event_name"]
	source = payload.get("source")
	return {
		"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
		"event": event,
		"source": str(source),
		"session_id": session_id,
		"path": str(path),
		"sha256": file_sha256(path),
	}


def write_record(record: dict, directory: Path) -> Path:
	"""Replace the session's record atomically."""
	directory.mkdir(parents=True, exist_ok=True)
	path = record_path(directory, record["session_id"])
	handle, temporary = tempfile.mkstemp(prefix=".record-", dir=directory)
	try:
		with os.fdopen(handle, "w", encoding="utf-8") as stream:
			stream.write(json.dumps(record, ensure_ascii=False) + "\n")
		os.replace(temporary, path)
	except BaseException:
		try:
			os.unlink(temporary)
		except OSError:
			pass
		raise
	return path


def main() -> int:
	try:
		raw = sys.stdin.read()
		payload = json.loads(raw) if raw.strip() else {}
		if isinstance(payload, dict):
			record = build_record(payload, datetime.now(timezone.utc), os.environ.get("CLAUDE_PROJECT_DIR"))
			if record is not None:
				write_record(record, record_dir())
	except Exception:  # noqa: BLE001 - recording must never affect the session or the config change
		pass
	return 0


if __name__ == "__main__":
	sys.exit(main())
