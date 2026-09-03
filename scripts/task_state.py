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


def _task_segment(raw_value: Any, field_name: str, issue_id: Any) -> str | None:
	segment = "" if raw_value is None else str(raw_value)
	if segment in ("", ".", ".."):
		_log_task_state_write_fail(issue_id, f"invalid_{field_name}:{segment or 'empty'}")
		return None
	if len(segment) >= 2 and segment[1] == ":" and segment[0].isalpha():
		_log_task_state_write_fail(issue_id, f"invalid_{field_name}:{segment}")
		return None
	if Path(segment).name != segment or "\\" in segment:
		_log_task_state_write_fail(issue_id, f"invalid_{field_name}:{segment}")
		return None
	return segment


def _task_wave_dir(wave_id: Any, issue_id: Any) -> Path | None:
	safe_wave_id = _task_segment(wave_id, "wave_id", issue_id)
	if safe_wave_id is None:
		return None
	return REPO_ROOT / TASKS_ROOT / safe_wave_id


def _task_path(wave_id: Any, issue_id: Any) -> Path | None:
	wave_dir = _task_wave_dir(wave_id, issue_id)
	safe_issue_id = _task_segment(issue_id, "issue_id", issue_id)
	if wave_dir is None or safe_issue_id is None:
		return None
	return wave_dir / f"{safe_issue_id}.json"


def _log_task_state_write_fail(issue_id: Any, reason: str) -> None:
	issue_token = str(issue_id or "unknown")
	reason_token = " ".join(str(reason).split()) or "unknown"
	print(f"TASK_STATE_WRITE_FAIL {issue_token} {reason_token}", file=sys.stderr)


def _task_symlink_target(task_dir: Path) -> Path | None:
	tasks_root = REPO_ROOT / TASKS_ROOT
	if tasks_root.is_symlink():
		return tasks_root
	if task_dir.is_symlink():
		return task_dir
	return None


def _atomic_write_json(path: Path, payload: Any, issue_id: Any) -> bool:
	tmp_path: Path | None = None
	try:
		symlink_target = _task_symlink_target(path.parent)
		if symlink_target is not None:
			raise OSError(f"refusing_to_traverse_symlink:{symlink_target}")
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
	path = _task_path(wave_id, issue_id)
	if path is None:
		return False
	return _atomic_write_json(path, payload, issue_id)


def read_task(wave_id: Any, issue_id: Any) -> dict[str, Any] | None:
	if not _task_files_enabled():
		return None
	path = _task_path(wave_id, issue_id)
	if path is None:
		return None
	if not path.exists():
		return None
	return _load_task_payload(path, issue_id)



def _candidate_blocker_tokens(
	wave_id: Any,
	completed_issue_id: Any,
	completed_issue_payload: dict[str, Any] | None = None,
) -> set[str]:
	candidate_values = {str(completed_issue_id)}
	completed_payload = completed_issue_payload
	if not isinstance(completed_payload, dict):
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


def _blocker_tokens_from_values(blockers: Any) -> set[str]:
	if not isinstance(blockers, list):
		return set()
	return {
		str(blocker)
		for blocker in blockers
		if blocker not in (None, "") and str(blocker)
	}


def _issue_reference_tokens(issue: Any) -> set[str]:
	if not isinstance(issue, dict):
		return set()
	tokens = set()
	issue_id = issue.get("id")
	if issue_id not in (None, ""):
		tokens.add(str(issue_id))
	github_issue = issue.get("github_issue")
	if github_issue not in (None, ""):
		tokens.add(str(github_issue))
	return tokens


def _issue_is_unblock_terminal(issue: Any) -> bool:
	if not isinstance(issue, dict):
		return False
	return str(issue.get("status") or "").strip() in TASK_STATE_UNBLOCK_TERMINAL_STATUSES


def unblock_dependents(
	wave_id: Any,
	completed_issue_id: Any,
	completed_issue_payload: dict[str, Any] | None = None,
) -> int:
	if not _task_files_enabled():
		return 0

	wave_dir = _task_wave_dir(wave_id, completed_issue_id)
	safe_completed_issue_id = _task_segment(completed_issue_id, "issue_id", completed_issue_id)
	if wave_dir is None or safe_completed_issue_id is None:
		return 0
	symlink_target = _task_symlink_target(wave_dir)
	if symlink_target is not None:
		_log_task_state_write_fail(completed_issue_id, f"refusing_to_traverse_symlink:{symlink_target}")
		return 0
	blocker_tokens = _candidate_blocker_tokens(
		wave_id,
		completed_issue_id,
		completed_issue_payload=completed_issue_payload,
	)
	count_unblocked = 0

	if wave_dir.is_dir() and blocker_tokens:
		completed_issue_filename = f"{safe_completed_issue_id}.json"
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
	for wave_index, wave in enumerate(state.get("waves", []), start=1):
		if not isinstance(wave, dict):
			continue
		wave_id = wave.get("wave", wave_index)
		issues = wave.get("issues", [])
		if not isinstance(issues, list):
			continue
		blocker_tokens_in_wave = set()
		for issue in issues:
			if not isinstance(issue, dict):
				continue
			blocker_tokens_in_wave.update(_blocker_tokens_from_values(issue.get("depends_on")))
			blocker_tokens_in_wave.update(_blocker_tokens_from_values(issue.get("reissue_depends_on")))
		terminal_issue_refs_to_unblock: list[tuple[Any, Any]] = []
		for issue in issues:
			if not isinstance(issue, dict):
				continue
			issue_id = issue.get("id")
			if issue_id in (None, ""):
				_log_task_state_write_fail(issue_id, f"missing_issue_id_for_wave_{wave_id}")
				continue
			if write_task(wave_id, issue_id, issue):
				written += 1
				if _issue_is_unblock_terminal(issue) and _issue_reference_tokens(issue) & blocker_tokens_in_wave:
					terminal_issue_refs_to_unblock.append((wave_id, issue_id))

		# The chunked orchestrator state stays authoritative and intentionally
		# does not persist mirror-only blocker pruning, so every mirror pass must
		# re-apply completed blockers that still appear in the authoritative state.
		for current_wave_id, current_issue_id in terminal_issue_refs_to_unblock:
			unblock_dependents(current_wave_id, current_issue_id)
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
