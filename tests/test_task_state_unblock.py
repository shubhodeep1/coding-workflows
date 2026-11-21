#!/usr/bin/env python3
"""Unit tests for task-state dependency unblocking."""

from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import task_state


def _set_task_state_root(root: Path, *, enabled: str) -> tuple[Path, str | None]:
	previous_root = task_state.REPO_ROOT
	previous_flag = os.environ.get("ORCH_TASK_FILES_ENABLED")
	task_state.REPO_ROOT = root
	os.environ["ORCH_TASK_FILES_ENABLED"] = enabled
	return previous_root, previous_flag


def _restore_task_state_root(previous_root: Path, previous_flag: str | None) -> None:
	task_state.REPO_ROOT = previous_root
	if previous_flag is None:
		os.environ.pop("ORCH_TASK_FILES_ENABLED", None)
	else:
		os.environ["ORCH_TASK_FILES_ENABLED"] = previous_flag


def test_unblock_dependents_updates_only_changed_files() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			assert task_state.write_task(1, "T1", {
				"id": "T1",
				"github_issue": 201,
				"status": "pending",
				"depends_on": ["T2", "T3"],
				"reissue_depends_on": [202, 999],
			})
			assert task_state.write_task(1, "T2", {"id": "T2", "github_issue": 202, "status": "merged"})
			assert task_state.write_task(1, "T3", {"id": "T3", "github_issue": 203, "status": "pending"})
			assert task_state.write_task(1, "T4", {
				"id": "T4",
				"github_issue": 204,
				"status": "pending",
				"depends_on": ["T5"],
			})
			assert task_state.write_task(1, "T5", {"id": "T5", "github_issue": 205, "status": "pending"})
			assert task_state.write_task(2, "T6", {
				"id": "T6",
				"github_issue": 206,
				"status": "pending",
				"depends_on": ["T2"],
			})

			unchanged_wave1_t4 = task_state.read_task(1, "T4")
			unchanged_wave1_t5 = task_state.read_task(1, "T5")
			unchanged_wave2_t6 = task_state.read_task(2, "T6")

			stderr = io.StringIO()
			with redirect_stderr(stderr):
				assert task_state.main(["unblock-dependents", "--wave-id", "1", "--completed-issue-id", "T2"]) == 0

			assert task_state.read_task(1, "T1") == {
				"depends_on": ["T3"],
				"github_issue": 201,
				"id": "T1",
				"reissue_depends_on": [999],
				"schema_version": "task_state.v1.json",
				"status": "pending",
			}
			assert task_state.read_task(1, "T3") == {
				"github_issue": 203,
				"id": "T3",
				"schema_version": "task_state.v1.json",
				"status": "pending",
			}
			assert task_state.read_task(1, "T4") == unchanged_wave1_t4
			assert task_state.read_task(1, "T5") == unchanged_wave1_t5
			assert task_state.read_task(2, "T6") == unchanged_wave2_t6
			assert "TASK_STATE_UNBLOCK 1 T2 1" in stderr.getvalue()
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def test_unblock_dependents_uses_supplied_completed_issue_payload_for_github_issue_tokens() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			assert task_state.write_task(1, "T1", {
				"id": "T1",
				"github_issue": 201,
				"status": "pending",
				"reissue_depends_on": [202, 999],
			})

			stderr = io.StringIO()
			with redirect_stderr(stderr):
				assert task_state.unblock_dependents(
					1,
					"T2",
					completed_issue_payload={
						"id": "T2",
						"github_issue": 202,
						"status": "merged",
					},
				) == 1

			assert task_state.read_task(1, "T1") == {
				"github_issue": 201,
				"id": "T1",
				"reissue_depends_on": [999],
				"schema_version": "task_state.v1.json",
				"status": "pending",
			}
			assert task_state.read_task(1, "T2") is None
			assert "TASK_STATE_UNBLOCK 1 T2 1" in stderr.getvalue()
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def test_unblock_dependents_rejects_symlinked_tasks_root() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		outside_root = root / "outside"
		outside_root.mkdir()
		(root / ".tasks").symlink_to(outside_root, target_is_directory=True)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			stderr = io.StringIO()
			with redirect_stderr(stderr):
				assert task_state.unblock_dependents(1, "T2") == 0
			assert (
				f"TASK_STATE_WRITE_FAIL T2 refusing_to_traverse_symlink:{root / '.tasks'}"
				in stderr.getvalue()
			)
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def main() -> int:
	test_funcs = [value for key, value in sorted(globals().items()) if key.startswith("test_") and callable(value)]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
