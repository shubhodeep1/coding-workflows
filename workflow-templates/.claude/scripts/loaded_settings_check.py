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

  loaded_settings_check.py [--session-id ID] [--settings PATH] [--before REV]
                           [--wait-seconds N]

`--session-id` defaults to `CLAUDE_CODE_SESSION_ID` (the session's transcript
id, which is the `session_id` every hook receives); `--settings` defaults to
`.claude/settings.json` in the current directory. `--before` names the
revision of the branch before the sync merge (the sync passes `HEAD^1` after
the merge commit, and `HEAD` while a conflicted merge is unfinished): the
check reads that revision's `settings.json` with `git show` and reports
whether it wired the recorder under `ConfigChange`. Claude Code runs
`ConfigChange` with the hooks loaded before a change, so on a branch whose
`settings.json` predates the recorder the merged file's reload can never be
recorded; the reason then says so instead of claiming the file was not
loaded, and also when no record exists at all (such a branch wires no
recorder at SessionStart either). It never changes the verdict. `REV` must be
a plain revision name such as `HEAD^1` (`_BEFORE_REV_RE`: no leading `-`, no
`:`, no whitespace), and `git show` gets `--end-of-options` before it: a value
that could be read as a `git` option exits 2 as a bad argument (PR #5283
operator review N1, where `--before=--output=<path>` made the allowlisted
check write a file).

Claude Code's file watcher applies a change, and runs `ConfigChange`, a few
seconds after the file changes on disk. So when the record is missing or names
another hash, the check re-reads it every half second for up to
`--wait-seconds` (default `LOADED_SETTINGS_CHECK_WAIT_SECONDS`, else 10) before
it reports "not current" (PR #5283 review round 2). It never waits when waiting
cannot change the answer: no session id, no readable `settings.json`, an
unreadable or malformed record, or a pre-recorder branch. `0` checks once.

Output is one JSON line: `current` (bool), `loaded_sha256` (from the record,
or null), `file_sha256` (the file now, or `absent`), `record` (the record
path, or null), `recorded_event`, `recorded_at`, `before_recorder_wired`
(true / false, or null without `--before` or when that revision's
`settings.json` cannot be read), and `reason`. Exit 0 when
current, 1 when not current, 2 on bad arguments. A missing session id, a
missing or unreadable record, a record for a different hash, or no readable
`settings.json` on disk (even when the record says `absent` too: there is no
wiring to confirm) is "not current": the check fails closed, and the caller hands its work to a fresh
session instead of pushing under wiring nobody confirmed. No API calls
(§15); local file reads and one local `git show` with `--before` only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path


RECORD_DIR_PARTS = (".claude", "loaded-settings")
DEFAULT_SETTINGS = Path(".claude") / "settings.json"
ABSENT = "absent"
_SESSION_FILE_RE = re.compile(r"[^A-Za-z0-9_.-]")
# A revision name for `--before`: no leading `-` (a `git` option), no `:` (it
# would change the `<rev>:<path>` object name), no whitespace.
_BEFORE_REV_RE = re.compile(r"[A-Za-z0-9_./^~@{}][A-Za-z0-9_./^~@{}-]*")
RECORDER_SCRIPT_NAME = "settings_load_recorder.py"
RECORDER_CONFIG_SOURCE = "project_settings"
NOT_LOADED_REASON = "the session has not loaded this settings.json"
NO_RECORD_REASON = "no record for this session: the settings load recorder hook has not run"
WAIT_SECONDS_ENV = "LOADED_SETTINGS_CHECK_WAIT_SECONDS"
DEFAULT_WAIT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.5
UNOBSERVABLE_REASON = (
	"the session has not recorded loading this settings.json: before the merge ({rev}) the branch's"
	" settings.json wired no ConfigChange recorder, and Claude Code runs ConfigChange with the hooks"
	" loaded before a change, so this session cannot record the merged file's reload"
)


def file_sha256(path: Path) -> str:
	try:
		return hashlib.sha256(path.read_bytes()).hexdigest()
	except OSError:
		return ABSENT


def _matches_project_settings(matcher) -> bool:
	if matcher in (None, "", "*"):
		return True
	if not isinstance(matcher, str):
		return False
	try:
		return re.fullmatch(matcher, RECORDER_CONFIG_SOURCE) is not None
	except re.error:
		return matcher == RECORDER_CONFIG_SOURCE


def recorder_wired(settings_data) -> bool:
	"""Whether a parsed settings.json runs the recorder on a `project_settings` ConfigChange."""
	if not isinstance(settings_data, dict):
		return False
	hooks = settings_data.get("hooks")
	entries = hooks.get("ConfigChange") if isinstance(hooks, dict) else None
	if not isinstance(entries, list):
		return False
	for entry in entries:
		if not isinstance(entry, dict) or not _matches_project_settings(entry.get("matcher")):
			continue
		inner = entry.get("hooks")
		if not isinstance(inner, list):
			continue
		for hook in inner:
			if isinstance(hook, dict) and RECORDER_SCRIPT_NAME in str(hook.get("command", "")):
				return True
	return False


def is_safe_before_rev(rev: str) -> bool:
	"""Whether `rev` is a plain revision name `git show` cannot read as an option."""
	return isinstance(rev, str) and _BEFORE_REV_RE.fullmatch(rev) is not None


def recorder_wired_at(rev: str, settings: Path) -> bool | None:
	"""Whether `rev`'s copy of `settings` wires the recorder; None when it cannot be read."""
	if not is_safe_before_rev(rev):
		return None
	directory = settings.parent
	try:
		shown = subprocess.run(
			["git", "-C", str(directory), "show", "--end-of-options", f"{rev}:./{settings.name}"],
			capture_output=True,
			# settings.json is UTF-8 whatever the locale; strict decoding keeps an undecodable revision null.
			encoding="utf-8",
			timeout=10,
			check=False,
		)
	except (OSError, subprocess.SubprocessError, ValueError):
		# ValueError covers UnicodeDecodeError from undecodable `git show` output.
		return None
	if shown.returncode != 0:
		return None
	try:
		return recorder_wired(json.loads(shown.stdout))
	except ValueError:
		return None


def check(session_id: str | None, settings: Path, directory: Path, before: str | None = None) -> dict:
	result: dict = {
		"current": False,
		"loaded_sha256": None,
		"file_sha256": file_sha256(settings),
		"record": None,
		"recorded_event": None,
		"recorded_at": None,
		"before_recorder_wired": recorder_wired_at(before, settings) if before else None,
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
		if result["before_recorder_wired"] is False:
			result["reason"] = UNOBSERVABLE_REASON.format(rev=before)
		else:
			result["reason"] = NO_RECORD_REASON
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
	if result["file_sha256"] == ABSENT:
		result["reason"] = "no readable settings.json to verify"
	elif record["sha256"] == result["file_sha256"]:
		result["current"] = True
		result["reason"] = "the session loaded this settings.json"
	elif result["before_recorder_wired"] is False:
		result["reason"] = UNOBSERVABLE_REASON.format(rev=before)
	else:
		result["reason"] = NOT_LOADED_REASON
	return result


def check_with_wait(
	session_id: str | None,
	settings: Path,
	directory: Path,
	before: str | None = None,
	wait_seconds: float = 0.0,
	clock=time.monotonic,
	pause=time.sleep,
) -> dict:
	"""`check`, re-run every POLL_INTERVAL_SECONDS for up to `wait_seconds`
	while the record is missing or names another hash: the only answers a
	late `ConfigChange` from the file watcher can still turn into "current"."""
	deadline = clock() + max(wait_seconds, 0.0)
	while True:
		result = check(session_id, settings, directory, before)
		if result["current"] or result["reason"] not in (NOT_LOADED_REASON, NO_RECORD_REASON):
			return result
		remaining = deadline - clock()
		if remaining <= 0:
			return result
		pause(min(POLL_INTERVAL_SECONDS, remaining))


def default_wait_seconds() -> float:
	raw = os.environ.get(WAIT_SECONDS_ENV)
	if raw is None or not raw.strip():
		return DEFAULT_WAIT_SECONDS
	try:
		value = float(raw)
	except ValueError:
		return DEFAULT_WAIT_SECONDS
	return value if math.isfinite(value) and value >= 0 else DEFAULT_WAIT_SECONDS


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
	parser.add_argument("--session-id", default=None)
	parser.add_argument("--settings", default=str(DEFAULT_SETTINGS))
	parser.add_argument("--before", default=None)
	parser.add_argument("--wait-seconds", type=float, default=None)
	parser.add_argument("--record-dir", default=None, help=argparse.SUPPRESS)
	args = parser.parse_args(argv)
	session_id = args.session_id if args.session_id is not None else os.environ.get("CLAUDE_CODE_SESSION_ID")
	directory = Path(args.record_dir) if args.record_dir else Path.home().joinpath(*RECORD_DIR_PARTS)
	wait_seconds = default_wait_seconds() if args.wait_seconds is None else args.wait_seconds
	if not math.isfinite(wait_seconds) or wait_seconds < 0:
		parser.error("--wait-seconds must be a finite number, 0 or more")
	if args.before is not None and not is_safe_before_rev(args.before):
		parser.error("--before must be a revision name such as HEAD^1, not a git option")
	result = check_with_wait(session_id, Path(args.settings), directory, args.before, wait_seconds)
	print(json.dumps(result, sort_keys=True))
	return 0 if result["current"] else 1


if __name__ == "__main__":
	sys.exit(main())
