#!/usr/bin/env python3
"""Mirror orchestrator wave issues into per-task JSON files."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


TASK_STATE_SCHEMA_VERSION = "task_state.v1.json"
REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_ROOT = ".tasks"
TASK_STATE_UNBLOCK_TERMINAL_STATUSES = frozenset({"merged", "closed", "skipped"})


def _task_files_enabled() -> bool:
	return os.environ.get("ORCH_TASK_FILES_ENABLED", "").strip().lower() == "true"


def _task_path(wave_id: Any, issue_id: Any) -> Path:
	return REPO_ROOT / TASKS_ROOT / str(wave_id) / f"{issue_id}.json"


def _log_task_state_write_fail(issue_id: Any, reason: str) -> None:
	issue_token = str(issue_id or "unknown")
	reason_token = " ".join(str(reason).split()) or "unknown"
	print(f"TASK_STATE_WRITE_FAIL {issue_token} {reason_token}", file=sys.stderr)


def _atomic_write_json(path: Path, payload: Any, issue_id: Any) -> bool:
	tmp_path: Path | None = None
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
		if path.exists() and path.is_symlink():
			raise OSError(f"refusing_to_overwrite_symlink:{path}")
		with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
			json.dump(payload, tmp, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False)
			tmp.write("\n")
			tmp_path = Path(tmp.name)
		os.replace(tmp_path, path)
		return True
	except (OSError, TypeError, ValueError) as exc:
		if tmp_path is not None:
			try:
				tmp_path.unlink(missing_ok=True)
			except OSError:
				pass
		_log_task_state_write_fail(issue_id, exc)
		return False


def _load_task_payload(path: Path, issue_id: Any) -> dict[str, Any] | None:
	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		_log_task_state_write_fail(issue_id, f"read_failed:{exc}")
		return None
	if not isinstance(payload, dict):
		_log_task_state_write_fail(issue_id, f"read_failed:expected_object_got_{type(payload).__name__}")
		return None
	return payload


def write_task(wave_id: Any, issue_id: Any, state_dict: dict[str, Any]) -> bool:
	if not _task_files_enabled():
		return False
	if issue_id in (None, ""):
		_log_task_state_write_fail(issue_id, "missing_issue_id")
		return False
	try:
		payload = dict(state_dict)
	except Exception as exc:
		_log_task_state_write_fail(issue_id, f"invalid_state_dict:{exc}")
		return False
	payload["schema_version"] = TASK_STATE_SCHEMA_VERSION
	return _atomic_write_json(_task_path(wave_id, issue_id), payload, issue_id)


def read_task(wave_id: Any, issue_id: Any) -> dict[str, Any] | None:
	if not _task_files_enabled():
		return None
	path = _task_path(wave_id, issue_id)
	if not path.exists():
		return None
	return _load_task_payload(path, issue_id)


def _candidate_blocker_tokens(wave_id: Any, completed_issue_id: Any) -> set[str]:
	candidate_values = {str(completed_issue_id)}
	completed_payload = read_task(wave_id, completed_issue_id)
	if isinstance(completed_payload, dict):
		github_issue = completed_payload.get("github_issue")
		if github_issue not in (None, ""):
			candidate_values.add(str(github_issue))
	return {value for value in candidate_values if value}


def _prune_blockers(blockers: Any, blocker_tokens: set[str]) -> tuple[Any, bool]:
	if not isinstance(blockers, list):
		return blockers, False
	filtered = [item for item in blockers if str(item) not in blocker_tokens]
	return filtered, filtered != blockers


def _issue_is_unblock_terminal(issue: Any) -> bool:
	if not isinstance(issue, dict):
		return False
	if issue.get("github_issue") in (None, ""):
		return False
	return str(issue.get("status") or "").strip() in TASK_STATE_UNBLOCK_TERMINAL_STATUSES


def unblock_dependents(wave_id: Any, completed_issue_id: Any) -> int:
	if not _task_files_enabled():
		return 0

	wave_dir = REPO_ROOT / TASKS_ROOT / str(wave_id)
	blocker_tokens = _candidate_blocker_tokens(wave_id, completed_issue_id)
	count_unblocked = 0

	if wave_dir.is_dir() and blocker_tokens:
		completed_issue_filename = f"{completed_issue_id}.json"
		for task_path in sorted(wave_dir.glob("*.json")):
			if task_path.name == completed_issue_filename:
				continue
			payload = _load_task_payload(task_path, task_path.stem)
			if payload is None:
				continue

			changed = False
			for field_name in ("depends_on", "reissue_depends_on"):
				filtered, field_changed = _prune_blockers(payload.get(field_name), blocker_tokens)
				if field_changed:
					payload[field_name] = filtered
					changed = True

			if changed and _atomic_write_json(task_path, payload, task_path.stem):
				count_unblocked += 1

	print(f"TASK_STATE_UNBLOCK {wave_id} {completed_issue_id} {count_unblocked}", file=sys.stderr)
	return count_unblocked


def mirror_state(state: dict[str, Any]) -> int:
	if not _task_files_enabled():
		return 0

	written = 0
	newly_terminal_issue_refs: list[tuple[Any, Any]] = []
	for wave_index, wave in enumerate(state.get("waves", []), start=1):
		if not isinstance(wave, dict):
			continue
		wave_id = wave.get("wave", wave_index)
		issues = wave.get("issues", [])
		if not isinstance(issues, list):
			continue
		for issue in issues:
			if not isinstance(issue, dict):
				continue
			issue_id = issue.get("id")
			if issue_id in (None, ""):
				_log_task_state_write_fail(issue_id, f"missing_issue_id_for_wave_{wave_id}")
				continue
			previous_issue_payload = read_task(wave_id, issue_id)
			if write_task(wave_id, issue_id, issue):
				written += 1
				if _issue_is_unblock_terminal(issue) and not _issue_is_unblock_terminal(previous_issue_payload):
					newly_terminal_issue_refs.append((wave_id, issue_id))

	for wave_id, issue_id in newly_terminal_issue_refs:
		unblock_dependents(wave_id, issue_id)
	return written


def _cmd_mirror_state(args: argparse.Namespace) -> int:
	state_path = Path(args.state_file)
	try:
		state = json.loads(state_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		print(f"mirror-state failed: {exc}", file=sys.stderr)
		return 1
	if not isinstance(state, dict):
		print("mirror-state failed: state file must contain a JSON object", file=sys.stderr)
		return 1
	mirror_state(state)
	return 0


def _cmd_unblock_dependents(args: argparse.Namespace) -> int:
	unblock_dependents(args.wave_id, args.completed_issue_id)
	return 0


def _build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	subparsers = parser.add_subparsers(dest="command", required=True)

	mirror_parser = subparsers.add_parser("mirror-state", help="Mirror wave issue state into .tasks/")
	mirror_parser.add_argument("--state-file", required=True)
	mirror_parser.set_defaults(func=_cmd_mirror_state)

	unblock_parser = subparsers.add_parser("unblock-dependents", help="Remove a completed issue from mirrored blockers")
	unblock_parser.add_argument("--wave-id", required=True)
	unblock_parser.add_argument("--completed-issue-id", required=True)
	unblock_parser.set_defaults(func=_cmd_unblock_dependents)

	return parser


def main(argv: list[str] | None = None) -> int:
	parser = _build_parser()
	args = parser.parse_args(argv)
	return int(args.func(args))


if __name__ == "__main__":
	raise SystemExit(main())
