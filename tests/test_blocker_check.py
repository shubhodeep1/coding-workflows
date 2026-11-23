#!/usr/bin/env python3
from __future__ import annotations

import io
import importlib.util
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BLOCKER_CHECK_PATH = REPO_ROOT / "scripts" / "blocker_check.py"
POLLER_TEST_PATH = REPO_ROOT / "tests" / "test_orchestrate_poll_process.py"
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


blocker_check = _load_module("blocker_check", BLOCKER_CHECK_PATH)
poller_tests = _load_module("test_orchestrate_poll_process_helpers", POLLER_TEST_PATH)


def _blocker_state() -> dict:
	return {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 1,
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "pending"},
					{"id": "issue-2", "github_issue": 11, "status": "pending"},
				],
			}
		],
		"dependency_edges": [{"from": "issue-1", "to": "issue-2"}],
		"issue_number_map": {"issue-1": 10, "issue-2": 11},
		"pending_issue_defs": {},
	}


def test_evaluate_blocker_eligibility_fails_open_without_dependency_metadata():
	state = _blocker_state()
	state.pop("dependency_edges")

	result = blocker_check.evaluate_blocker_eligibility(state, local_id="issue-2")

	assert result["eligible"] is True
	assert result["signal"] == "dispatch_eligible"
	assert result["reason"] == "no_dependency_metadata"
	assert result["metadata_present"] is False
	assert result["blockers"] == []


def test_evaluate_blocker_eligibility_allows_explicit_empty_dependency_metadata():
	state = _blocker_state()
	state["dependency_edges"] = []

	result = blocker_check.evaluate_blocker_eligibility(state, local_id="issue-2")

	assert result["eligible"] is True
	assert result["signal"] == "dispatch_eligible"
	assert result["reason"] == "no_incoming_blockers"
	assert result["metadata_present"] is True
	assert result["blockers"] == []


def test_evaluate_blocker_eligibility_defers_invalid_dependency_metadata():
	state = _blocker_state()
	state["dependency_edges"] = {"from": "issue-1", "to": "issue-2"}

	result = blocker_check.evaluate_blocker_eligibility(state, local_id="issue-2")

	assert result["eligible"] is False
	assert result["signal"] == "dispatch_deferred_blocker"
	assert result["reason"] == "invalid_dependency_metadata"
	assert result["metadata_present"] is True
	assert result["blockers"] == []


def test_evaluate_blocker_eligibility_blocks_non_terminal_dependency():
	state = _blocker_state()

	result = blocker_check.evaluate_blocker_eligibility(
		state,
		local_id="issue-2",
		candidate_details={
			"10": {
				"state": "open",
				"labels": ["ai:implementing"],
				"linked_pr": None,
			}
		},
	)

	assert result["eligible"] is False
	assert result["signal"] == "dispatch_deferred_blocker"
	assert result["reason"] == "blocked_by_dependency"
	assert result["blockers"] == [
		{
			"local_id": "issue-1",
			"github_issue": 10,
			"terminal": False,
			"status": "in_progress",
			"source": "default_in_progress",
		}
	]


def test_evaluate_blocker_eligibility_allows_terminal_merged_dependency():
	state = _blocker_state()

	result = blocker_check.evaluate_blocker_eligibility(
		state,
		local_id="issue-2",
		candidate_details={
			"10": {
				"state": "open",
				"labels": ["ai:ready-to-merge"],
				"linked_pr": {
					"state": "MERGED",
					"merged": True,
				},
			}
		},
	)

	assert result["eligible"] is True
	assert result["signal"] == "dispatch_eligible"
	assert result["reason"] == "all_blockers_terminal"
	assert result["blockers"] == [
		{
			"local_id": "issue-1",
			"github_issue": 10,
			"terminal": True,
			"status": "merged",
			"source": "linked_pr_merged",
		}
	]


def test_evaluate_blocker_eligibility_uses_issue_number_map_when_wave_entry_missing():
	state = _blocker_state()
	state["waves"][0]["issues"] = [
		{"id": "issue-2", "github_issue": 11, "status": "pending"},
	]
	state["issue_number_map"] = {"issue-1": 10, "issue-2": 11}

	result = blocker_check.evaluate_blocker_eligibility(
		state,
		local_id="issue-2",
		candidate_details={
			"10": {
				"state": "open",
				"labels": ["ai:merged"],
				"linked_pr": None,
			}
		},
	)

	assert result["eligible"] is True
	assert result["signal"] == "dispatch_eligible"
	assert result["reason"] == "all_blockers_terminal"
	assert result["blockers"] == [
		{
			"local_id": "issue-1",
			"github_issue": 10,
			"terminal": True,
			"status": "merged",
			"source": "label_ai_merged",
		}
	]


def test_evaluate_blocker_eligibility_defers_when_blocker_mapping_is_missing():
	state = _blocker_state()
	state["waves"][0]["issues"] = [
		{"id": "issue-2", "github_issue": 11, "status": "pending"},
	]
	state["issue_number_map"] = {"issue-2": 11}

	result = blocker_check.evaluate_blocker_eligibility(state, local_id="issue-2")

	assert result["eligible"] is False
	assert result["signal"] == "dispatch_deferred_blocker"
	assert result["reason"] == "unresolved_blocker_mapping"
	assert result["blockers"] == [
		{
			"local_id": "issue-1",
			"github_issue": None,
			"terminal": False,
			"status": "missing",
			"source": "missing_wave_entry",
		}
	]


def test_evaluate_blocker_eligibility_allows_not_created_dependency():
	state = _blocker_state()
	state["waves"][0]["issues"][0] = {
		"id": "issue-1",
		"github_issue": None,
		"status": "not_created",
	}

	result = blocker_check.evaluate_blocker_eligibility(state, local_id="issue-2")

	assert result["eligible"] is True
	assert result["signal"] == "dispatch_eligible"
	assert result["reason"] == "all_blockers_terminal"
	assert result["blockers"] == [
		{
			"local_id": "issue-1",
			"github_issue": None,
			"terminal": True,
			"status": "not_created",
			"source": "stored_terminal",
		}
	]


def test_evaluate_blocker_eligibility_blocks_dependency_not_yet_created():
	state = _blocker_state()
	state["waves"][0]["issues"][0] = {
		"id": "issue-1",
		"github_issue": None,
		"status": "pending",
	}

	result = blocker_check.evaluate_blocker_eligibility(state, local_id="issue-2")

	assert result["eligible"] is False
	assert result["signal"] == "dispatch_deferred_blocker"
	assert result["reason"] == "blocked_by_dependency"
	assert result["blockers"] == [
		{
			"local_id": "issue-1",
			"github_issue": None,
			"terminal": False,
			"status": "not_created",
			"source": "no_github_issue",
		}
	]


def test_main_returns_structured_state_load_failure():
	missing_state = REPO_ROOT / "tests" / "missing-blocker-state.json"
	stdout = io.StringIO()

	with redirect_stdout(stdout):
		rc = blocker_check.main(["--state-file", str(missing_state), "--local-id", "issue-2"])

	result = json.loads(stdout.getvalue())
	assert rc == 0
	assert result["eligible"] is False
	assert result["signal"] == "dispatch_deferred_blocker"
	assert result["reason"] == "state_load_failed"
	assert result["metadata_present"] is False
	assert "FileNotFoundError:" in result["detail"]


def test_main_returns_structured_candidate_details_error():
	state = _blocker_state()
	stdout = io.StringIO()

	with tempfile.TemporaryDirectory() as td:
		state_file = Path(td) / "state.json"
		state_file.write_text(json.dumps(state), encoding="utf-8")

		with redirect_stdout(stdout):
			rc = blocker_check.main(
				[
					"--state-file",
					str(state_file),
					"--local-id",
					"issue-2",
					"--candidate-details-json",
					"[]",
				]
			)

	result = json.loads(stdout.getvalue())
	assert rc == 0
	assert result["eligible"] is False
	assert result["signal"] == "dispatch_deferred_blocker"
	assert result["reason"] == "candidate_details_invalid"
	assert result["metadata_present"] is False
	assert "ValueError:" in result["detail"]


def test_next_wave_dispatch_defers_blocked_issue_until_future_tick():
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 3,
		"total_waves": 2,
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-2", "github_issue": None, "status": "pending"},
					{"id": "issue-3", "github_issue": None, "status": "pending"},
				],
			},
		],
		"dependency_edges": [{"from": "issue-2", "to": "issue-3"}],
		"issue_number_map": {"issue-1": 10},
		"pending_issue_defs": {
			"issue-2": {"title": "Wave 2 blocker", "body": "body 2", "priority": 1},
			"issue-3": {"title": "Wave 2 blocked", "body": "body 3", "priority": 2},
		},
		"integration_branch": "",
		"final_merge_strategy": "squash",
		"final_merge_pr": None,
		"final_merge_status": "pending",
	}

	result = poller_tests._run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
	)

	assert result["latest_state"]["current_wave"] == 2
	assert result["latest_state"]["issue_number_map"]["issue-2"] == 900
	assert "issue-3" not in result["latest_state"]["issue_number_map"]
	assert "issue-3" in result["latest_state"]["pending_issue_defs"]
	assert "dispatch_deferred_blocker local_id=issue-3 wave=2 reason=blocked_by_dependency" in result["stdout"]
	assert result.get("created_issues", []) == [
		{
			"number": 900,
			"title": "Wave 2 blocker",
			"labels": ["ai:clarification", "ai:orchestrator-managed"],
		}
	]


def test_current_wave_deferred_creation_uses_live_blocker_truth():
	state = {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": 2,
		"total_waves": 1,
		"current_wave": 1,
		"judge_cycle": 0,
		"recovery_attempted": False,
		"review_blocked_retries": {},
		"status": "in_progress",
		"waves": [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-2", "github_issue": 10, "status": "pending"},
					{"id": "issue-3", "github_issue": None, "status": "pending"},
				],
			},
		],
		"dependency_edges": [{"from": "issue-2", "to": "issue-3"}],
		"issue_number_map": {"issue-2": 10},
		"pending_issue_defs": {
			"issue-3": {"title": "Current wave blocked", "body": "body 3", "priority": 2},
		},
		"integration_branch": "",
		"final_merge_strategy": "squash",
		"final_merge_pr": None,
		"final_merge_status": "pending",
	}

	result = poller_tests._run_poller(
		state=state,
		enable_validation="false",
		max_validate_cycles="3",
		issue_labels={10: ["ai:merged"]},
	)

	assert result["latest_state"]["issue_number_map"]["issue-3"] == 900
	assert "issue-3" not in result["latest_state"]["pending_issue_defs"]
	assert "dispatch_deferred_blocker local_id=issue-3" not in result["stdout"]
	assert result.get("created_issues", []) == [
		{
			"number": 900,
			"title": "Current wave blocked",
			"labels": ["ai:clarification", "ai:orchestrator-managed"],
		}
	]


def main() -> int:
	selected_names = list(sys.argv[1:])
	tests_by_name = {
		name: func
		for name, func in sorted(globals().items())
		if name.startswith("test_") and callable(func)
	}
	if selected_names:
		missing = [name for name in selected_names if name not in tests_by_name]
		for name in missing:
			print(f"  FAIL  {name}: unknown test name", flush=True)
		if missing:
			return 1
		test_funcs = [tests_by_name[name] for name in selected_names]
	else:
		test_funcs = list(tests_by_name.values())

	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}", flush=True)
			passed += 1
		except Exception as exc:
			print(f"  FAIL  {name}: {exc}", flush=True)
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total", flush=True)
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
