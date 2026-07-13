#!/usr/bin/env python3
"""Unit tests for scripts/task_state.py."""

from __future__ import annotations

import json
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


def test_write_and_read_task_round_trip_with_schema_version() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			payload = {
				"id": "issue-1",
				"github_issue": 101,
				"status": "pending",
				"depends_on": ["issue-0"],
			}
			assert task_state.write_task(1, "issue-1", payload) is True

			path = root / ".tasks" / "1" / "issue-1.json"
			stored = json.loads(path.read_text(encoding="utf-8"))
			assert stored == {
				"depends_on": ["issue-0"],
				"github_issue": 101,
				"id": "issue-1",
				"schema_version": "task_state.v1.json",
				"status": "pending",
			}
			assert task_state.read_task(1, "issue-1") == stored
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def test_write_task_is_noop_when_flag_disabled() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="false")
		try:
			assert task_state.write_task(1, "issue-1", {"id": "issue-1", "status": "pending"}) is False
			assert task_state.read_task(1, "issue-1") is None
			assert not (root / ".tasks").exists()
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def test_write_task_preserves_existing_file_on_replace_failure() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		original_replace = task_state.os.replace
		try:
			assert task_state.write_task(1, "issue-1", {"id": "issue-1", "status": "pending"}) is True
			path = root / ".tasks" / "1" / "issue-1.json"
			original_bytes = path.read_bytes()

			def _boom(_src: str | os.PathLike[str], _dst: str | os.PathLike[str]) -> None:
				raise OSError("forced_replace_failure")

			task_state.os.replace = _boom
			assert task_state.write_task(1, "issue-1", {"id": "issue-1", "status": "merged"}) is False
			assert path.read_bytes() == original_bytes
			assert len(list(path.parent.glob("*.json"))) == 1
		finally:
			task_state.os.replace = original_replace
			_restore_task_state_root(previous_root, previous_flag)


def test_write_task_rejects_path_escape_issue_id() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			stderr = io.StringIO()
			with redirect_stderr(stderr):
				assert task_state.write_task(1, "../issue-1", {"id": "../issue-1", "status": "pending"}) is False
			assert not (root / ".tasks").exists()
			assert "TASK_STATE_WRITE_FAIL ../issue-1 invalid_issue_id:../issue-1" in stderr.getvalue()
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def test_unblock_dependents_rejects_path_escape_wave_id() -> None:
	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			assert task_state.write_task(1, "issue-1", {"id": "issue-1", "github_issue": 101, "status": "merged"})
			stderr = io.StringIO()
			with redirect_stderr(stderr):
				assert task_state.unblock_dependents("../1", "issue-1") == 0
			assert task_state.read_task(1, "issue-1") == {
				"github_issue": 101,
				"id": "issue-1",
				"schema_version": "task_state.v1.json",
				"status": "merged",
			}
			assert "TASK_STATE_WRITE_FAIL issue-1 invalid_wave_id:../1" in stderr.getvalue()
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def test_mirror_state_cli_matches_state_issue_payloads() -> None:
	state = {
		"schema_version": "orchestrate_state.v1",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 101, "status": "pending"},
					{"id": "issue-2", "github_issue": None, "status": "not_created", "depends_on": ["issue-1"]},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-3", "github_issue": 103, "status": "ready-to-merge", "reissue_depends_on": [101]},
				],
			},
		],
	}

	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		state_path = root / "state.json"
		state_path.write_text(json.dumps(state), encoding="utf-8")
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			assert task_state.main(["mirror-state", "--state-file", str(state_path)]) == 0

			actual = {}
			for task_file in sorted((root / ".tasks").glob("*/*.json")):
				actual[f"{task_file.parent.name}/{task_file.name}"] = json.loads(task_file.read_text(encoding="utf-8"))

			expected = {
				"1/issue-1.json": {
					"github_issue": 101,
					"id": "issue-1",
					"schema_version": "task_state.v1.json",
					"status": "pending",
				},
				"1/issue-2.json": {
					"depends_on": ["issue-1"],
					"github_issue": None,
					"id": "issue-2",
					"schema_version": "task_state.v1.json",
					"status": "not_created",
				},
				"2/issue-3.json": {
					"github_issue": 103,
					"id": "issue-3",
					"reissue_depends_on": [101],
					"schema_version": "task_state.v1.json",
					"status": "ready-to-merge",
				},
			}
			assert actual == expected
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def test_mirror_state_unblocks_dependents_after_writing_newly_terminal_tasks() -> None:
	state = {
		"schema_version": "orchestrate_state.v1",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 101, "status": "merged"},
					{
						"id": "issue-2",
						"github_issue": 102,
						"status": "pending",
						"depends_on": ["issue-1"],
						"reissue_depends_on": [101, 999],
					},
				],
			},
		],
	}

	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			assert task_state.mirror_state(state) == 2
			assert task_state.read_task(1, "issue-1") == {
				"github_issue": 101,
				"id": "issue-1",
				"schema_version": "task_state.v1.json",
				"status": "merged",
			}
			assert task_state.read_task(1, "issue-2") == {
				"depends_on": [],
				"github_issue": 102,
				"id": "issue-2",
				"reissue_depends_on": [999],
				"schema_version": "task_state.v1.json",
				"status": "pending",
			}
		finally:
			_restore_task_state_root(previous_root, previous_flag)


def test_mirror_state_unblocks_skipped_issue_without_github_issue() -> None:
	state = {
		"schema_version": "orchestrate_state.v1",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": None, "status": "skipped"},
					{
						"id": "issue-2",
						"github_issue": 102,
						"status": "pending",
						"depends_on": ["issue-1"],
					},
				],
			},
		],
	}

	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root, previous_flag = _set_task_state_root(root, enabled="true")
		try:
			assert task_state.mirror_state(state) == 2
			assert task_state.read_task(1, "issue-2") == {
				"depends_on": [],
				"github_issue": 102,
				"id": "issue-2",
				"schema_version": "task_state.v1.json",
				"status": "pending",
			}
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
