#!/usr/bin/env python3
"""Tell whether this session has loaded the working tree's `.claude/settings.json`.

Implements the checking half of the Claude-asset sync's settings check
(issue #5259, `.claude/commands/implement-plan-claude.md`, "Claude-asset
sync" step 6 and "Settings restart"). `.claude/hooks/settings_load_recorder.py`
writes the sha256 of the `settings.json` the session loaded at start, or that
the file watcher last reported through `ConfigChange`, to
`~/.claude/loaded-settings/<session id>.json`. This script compares that
record with the file on disk now.

Usage:

  loaded_settings_check.py [--session-id ID] [--settings PATH]

`--session-id` defaults to `CLAUDE_CODE_SESSION_ID` (the session's transcript
id, which is the `session_id` every hook receives); `--settings` defaults to
`.claude/settings.json` in the current directory.

Output is one JSON line: `current` (bool), `loaded_sha256` (from the record,
or null), `file_sha256` (the file now, or `absent`), `record` (the record
path, or null), `recorded_event`, `recorded_at`, and `reason`. Exit 0 when
current, 1 when not current, 2 on bad arguments. A missing session id, a
missing or unreadable record, or a record for a different hash is "not
current": the check fails closed, and the caller hands its work to a fresh
session instead of pushing under wiring nobody confirmed. No API calls
(§15); local file reads only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


RECORD_DIR_PARTS = (".claude", "loaded-settings")
DEFAULT_SETTINGS = Path(".claude") / "settings.json"
ABSENT = "absent"
_SESSION_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def file_sha256(path: Path) -> str:
	try:
		return hashlib.sha256(path.read_bytes()).hexdigest()
	except OSError:
		return ABSENT


def check(session_id: str | None, settings: Path, directory: Path) -> dict:
	result: dict = {
		"current": False,
		"loaded_sha256": None,
		"file_sha256": file_sha256(settings),
		"record": None,
		"recorded_event": None,
		"recorded_at": None,
		"reason": "",
	}
	if not session_id:
		result["reason"] = "no session id: pass --session-id or set CLAUDE_CODE_SESSION_ID"
		return result
	path = directory / f"{_SESSION_FILE_RE.sub('_', session_id)[:120]}.json"
	result["record"] = str(path)
	try:
		record = json.loads(path.read_text(encoding="utf-8"))
	except FileNotFoundError:
		result["reason"] = "no record for this session: the settings load recorder hook has not run"
		return result
	except (OSError, ValueError):
		result["reason"] = "record unreadable"
		return result
	if not isinstance(record, dict) or not isinstance(record.get("sha256"), str):
		result["reason"] = "record malformed"
		return result
	result["loaded_sha256"] = record["sha256"]
	result["recorded_event"] = record.get("event")
	result["recorded_at"] = record.get("ts")
	if record["sha256"] == result["file_sha256"]:
		result["current"] = True
		result["reason"] = "the session loaded this settings.json"
	else:
		result["reason"] = "the session has not loaded this settings.json"
	return result


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	parser.add_argument("--session-id", default=None)
	parser.add_argument("--settings", default=str(DEFAULT_SETTINGS))
	parser.add_argument("--record-dir", default=None, help=argparse.SUPPRESS)
	args = parser.parse_args(argv)
	session_id = args.session_id if args.session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID")
	directory = Path(args.record_dir) if args.record_dir else Path.home().joinpath(*RECORD_DIR_PARTS)
	result = check(session_id, Path(args.settings), directory)
	print(json.dumps(result, sort_keys=True))
	return 0 if result["current"] else 1


if __name__ == "__main__":
	sys.exit(main())
