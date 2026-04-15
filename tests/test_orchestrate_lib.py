#!/usr/bin/env python3
"""Unit tests for orchestrate_lib.py — wave computation, status checks, and state schema."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

# Add scripts/ to path so we can import orchestrate_lib
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import orchestrate_lib


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict) -> None:
	with path.open("w", encoding="utf-8") as f:
		json.dump(data, f)


def _make_decomposition(
	issues: list[dict] | None = None,
	edges: list[dict] | None = None,
) -> dict:
	"""Build a minimal valid decomposition."""
	if issues is None:
		issues = [
			{"id": "issue-1", "title": "First task", "body": "Do the first thing", "priority": 1},
			{"id": "issue-2", "title": "Second task", "body": "Do the second thing", "priority": 2},
		]
	if edges is None:
		edges = []
	return {
		"schema_version": "orchestrate_decomposition.v1",
		"project_title": "Test Project",
		"project_summary": "A test project for unit tests",
		"issues": issues,
		"dependency_edges": edges,
	}


def _make_state(
	waves: list[list[dict]] | None = None,
	current_wave: int = 1,
	review_blocked_retries: dict | None = None,
) -> dict:
	"""Build a minimal valid tracking state."""
	if waves is None:
		waves = [
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "pending"},
					{"id": "issue-2", "github_issue": 11, "status": "pending"},
				],
			}
		]
	return {
		"schema_version": "orchestrate_state.v1",
		"project_title": "Test Project",
		"total_issues": sum(len(w["issues"]) for w in waves),
		"total_waves": len(waves),
		"current_wave": current_wave,
		"judge_cycle": 0,
		"recovery_attempted": False,
		"review_blocked_retries": review_blocked_retries or {},
		"status": "in_progress",
		"waves": waves,
		"dependency_edges": [],
		"issue_number_map": {},
		"pending_issue_defs": {},
		"integration_branch": "",
		"final_merge_strategy": "squash",
		"final_merge_pr": None,
		"final_merge_status": "pending",
	}


# ---------------------------------------------------------------------------
# Tests: validation
# ---------------------------------------------------------------------------

def test_validate_decomposition_valid():
	data = _make_decomposition()
	result = orchestrate_lib.validate_decomposition(data)
	assert result["project_title"] == "Test Project"


def test_validate_decomposition_invalid_schema():
	data = _make_decomposition()
	data["schema_version"] = "wrong"
	try:
		orchestrate_lib.validate_decomposition(data)
		assert False, "Should have raised OrchestrateError"
	except orchestrate_lib.OrchestrateError:
		pass


def test_validate_decomposition_missing_schema_version_legacy_accepted():
	data = _make_decomposition()
	data.pop("schema_version")
	result = orchestrate_lib.validate_decomposition(data)
	assert result["schema_version"] == "orchestrate_decomposition.v1"


def test_validate_decomposition_null_schema_version_legacy_accepted():
	data = _make_decomposition()
	data["schema_version"] = None
	result = orchestrate_lib.validate_decomposition(data)
	assert result["schema_version"] == "orchestrate_decomposition.v1"


def test_validate_decomposition_duplicate_ids():
	issues = [
		{"id": "dup", "title": "A", "body": "B", "priority": 1},
		{"id": "dup", "title": "C", "body": "D", "priority": 2},
	]
	try:
		orchestrate_lib.validate_decomposition(_make_decomposition(issues=issues))
		assert False, "Should have raised OrchestrateError"
	except orchestrate_lib.OrchestrateError as e:
		assert "Duplicate" in str(e)


def test_validate_decomposition_cycle_detection():
	issues = [
		{"id": "a", "title": "A", "body": "B", "priority": 1},
		{"id": "b", "title": "C", "body": "D", "priority": 2},
	]
	edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]
	try:
		orchestrate_lib.validate_decomposition(_make_decomposition(issues=issues, edges=edges))
		assert False, "Should have raised OrchestrateError"
	except orchestrate_lib.OrchestrateError as e:
		assert "cycle" in str(e).lower()


# ---------------------------------------------------------------------------
# Tests: wave computation
# ---------------------------------------------------------------------------

def test_compute_waves_no_deps():
	data = _make_decomposition()
	waves = orchestrate_lib.compute_waves(data)
	assert len(waves) == 1
	assert len(waves[0]) == 2


def test_compute_waves_with_deps():
	issues = [
		{"id": "a", "title": "A", "body": "A body", "priority": 1},
		{"id": "b", "title": "B", "body": "B body", "priority": 2},
		{"id": "c", "title": "C", "body": "C body", "priority": 3},
	]
	edges = [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}]
	data = _make_decomposition(issues=issues, edges=edges)
	waves = orchestrate_lib.compute_waves(data)
	assert len(waves) == 3
	assert waves[0][0]["id"] == "a"
	assert waves[1][0]["id"] == "b"
	assert waves[2][0]["id"] == "c"


def test_compute_waves_parallel_deps():
	issues = [
		{"id": "root", "title": "Root", "body": "Root body", "priority": 1},
		{"id": "child-1", "title": "Child 1", "body": "Body", "priority": 2},
		{"id": "child-2", "title": "Child 2", "body": "Body", "priority": 3},
	]
	edges = [{"from": "root", "to": "child-1"}, {"from": "root", "to": "child-2"}]
	data = _make_decomposition(issues=issues, edges=edges)
	waves = orchestrate_lib.compute_waves(data)
	assert len(waves) == 2
	assert len(waves[0]) == 1  # root
	assert len(waves[1]) == 2  # child-1, child-2


# ---------------------------------------------------------------------------
# Tests: check-wave-status
# ---------------------------------------------------------------------------

def _run_check_wave_status(state: dict, labels: dict[str, list[str]]) -> dict:
	"""Run check-wave-status via the CLI and return parsed JSON."""
	with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
		json.dump(state, f)
		state_path = f.name

	import io
	from contextlib import redirect_stdout

	try:
		buf = io.StringIO()
		with redirect_stdout(buf):
			orchestrate_lib.cmd_check_wave_status(
				type("Args", (), {"state_file": state_path, "labels_json": json.dumps(labels)})()
			)
		return json.loads(buf.getvalue())
	finally:
		os.unlink(state_path)


def _run_check_stalls(
	state: dict,
	labels: dict[str, list[str]],
	threshold_minutes: int = 60,
	now_ts: int = 2000,
	max_recoveries: int = 5,
	phase_thresholds_json: str | None = None,
	stall_judge_trigger_count: int = 0,
	enable_stall_judge: str = "false",
	allow_human_terminalization: str = "false",
) -> dict:
	"""Run check-stalls via the CLI and return parsed JSON."""
	with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
		json.dump(state, f)
		state_path = f.name

	import io
	from contextlib import redirect_stdout

	try:
		buf = io.StringIO()
		with redirect_stdout(buf):
			orchestrate_lib.cmd_check_stalls(
				type(
					"Args",
					(),
					{
						"state_file": state_path,
						"labels_json": json.dumps(labels),
						"threshold_minutes": str(threshold_minutes),
						"max_recoveries": str(max_recoveries),
						"phase_thresholds_json": phase_thresholds_json,
					"now_ts": str(now_ts),
					"stall_judge_trigger_count": str(stall_judge_trigger_count),
					"enable_stall_judge": enable_stall_judge,
					"allow_human_terminalization": allow_human_terminalization,
				},
			)()
		)
		return json.loads(buf.getvalue())
	finally:
		os.unlink(state_path)


def test_check_wave_status_all_merged():
	state = _make_state()
	labels = {"10": ["ai:merged"], "11": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is True
	assert result["any_failed"] is False
	assert result["any_review_blocked"] is False
	assert result["project_complete"] is True
	assert all(i["status"] == "merged" for i in result["issues"])


def test_check_wave_status_some_in_progress():
	state = _make_state()
	labels = {"10": ["ai:merged"], "11": ["ai:implementing"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is False
	assert result["any_review_blocked"] is False


def test_check_wave_status_ready_to_merge():
	state = _make_state()
	labels = {"10": ["ai:ready-to-merge"], "11": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is False
	issues_by_gh = {i["github_issue"]: i for i in result["issues"]}
	assert issues_by_gh[10]["status"] == "ready-to-merge"
	assert issues_by_gh[11]["status"] == "merged"


def test_check_wave_status_review_blocked():
	state = _make_state()
	labels = {"10": ["ai:review-blocked"], "11": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is False
	assert result["any_review_blocked"] is True
	assert result["any_failed"] is False
	issues_by_gh = {i["github_issue"]: i for i in result["issues"]}
	assert issues_by_gh[10]["status"] == "review-blocked"


def test_check_wave_status_review_blocked_and_failed():
	state = _make_state()
	labels = {"10": ["ai:review-blocked"], "11": ["ai:closed"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is False
	assert result["any_review_blocked"] is True
	assert result["any_failed"] is True


def test_check_wave_status_all_review_blocked():
	state = _make_state()
	labels = {"10": ["ai:review-blocked"], "11": ["ai:review-blocked"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is False
	assert result["any_review_blocked"] is True
	assert all(i["status"] == "review-blocked" for i in result["issues"])


def test_check_wave_status_no_labels_means_in_progress():
	state = _make_state()
	labels = {"10": [], "11": []}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is False
	assert result["any_review_blocked"] is False
	assert all(i["status"] == "in_progress" for i in result["issues"])


def test_check_wave_status_multi_wave_not_project_complete():
	waves = [
		{"wave": 1, "issues": [{"id": "a", "github_issue": 10, "status": "pending"}]},
		{"wave": 2, "issues": [{"id": "b", "github_issue": 11, "status": "pending"}]},
	]
	state = _make_state(waves=waves, current_wave=1)
	labels = {"10": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is True
	assert result["project_complete"] is False  # wave 2 still exists


def test_check_wave_status_closed_counts_as_failed():
	state = _make_state()
	labels = {"10": ["ai:closed"], "11": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["any_failed"] is True
	issues_by_gh = {i["github_issue"]: i for i in result["issues"]}
	assert issues_by_gh[10]["status"] == "closed"


def test_check_wave_status_null_github_issue_means_not_created():
	"""Issues with github_issue: null should be reported as not_created."""
	waves = [
		{
			"wave": 1,
			"issues": [
				{"id": "task-a", "github_issue": None, "status": "not_created"},
				{"id": "task-b", "github_issue": None, "status": "not_created"},
			],
		}
	]
	state = _make_state(waves=waves, current_wave=1)
	labels = {}  # no labels since no issues exist
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is False
	assert result["any_not_created"] is True
	assert all(i["status"] == "not_created" for i in result["issues"])


def test_check_wave_status_mixed_null_and_real_issues():
	"""Mix of created and uncreated issues should report correctly."""
	waves = [
		{
			"wave": 1,
			"issues": [
				{"id": "task-a", "github_issue": 10, "status": "pending"},
				{"id": "task-b", "github_issue": None, "status": "not_created"},
			],
		}
	]
	state = _make_state(waves=waves, current_wave=1)
	labels = {"10": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is False
	assert result["any_not_created"] is True
	issues_by_id = {i["id"]: i for i in result["issues"]}
	assert issues_by_id["task-a"]["status"] == "merged"
	assert issues_by_id["task-b"]["status"] == "not_created"

def test_detect_stalls_selects_run_stall_judge_at_trigger_threshold():
	state = _make_state()
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 2
	labels = {"10": ["ai:implementing"], "11": ["ai:merged"]}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=8 * 60 * 60,
		max_recoveries=5,
		stall_judge_trigger_count=2,
		enable_stall_judge=True,
	)

	assert len(stalls) == 1
	assert stalls[0]["github_issue"] == 10
	assert stalls[0]["recovery_action"] == "run_stall_judge"


def test_detect_stalls_uses_ladder_when_stall_judge_disabled_for_implementing_phase():
	state = _make_state()
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 2
	labels = {"10": ["ai:implementing"], "11": ["ai:merged"]}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=8 * 60 * 60,
		max_recoveries=5,
		stall_judge_trigger_count=2,
		enable_stall_judge=False,
	)

	assert len(stalls) == 1
	assert stalls[0]["recovery_action"] == orchestrate_lib.STALL_RECOVERY_ACTIONS["ai:implementing"][1]


def test_detect_stalls_allows_human_terminalization_when_explicitly_enabled():
	state = _make_state()
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 2
	labels = {"10": ["ai:implementing"], "11": ["ai:merged"]}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=8 * 60 * 60,
		max_recoveries=5,
		stall_judge_trigger_count=2,
		enable_stall_judge=False,
		allow_human_terminalization=True,
	)

	assert len(stalls) == 1
	assert stalls[0]["recovery_action"] == "escalate_human"


def test_detect_stalls_skips_needs_human_label():
	state = _make_state()
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 3
	labels = {"10": ["ai:implementing", "ai:needs-human"], "11": ["ai:merged"]}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=8 * 60 * 60,
		max_recoveries=5,
		stall_judge_trigger_count=2,
		enable_stall_judge=True,
	)

	assert stalls == []


def test_detect_stalls_max_recoveries_still_skips_with_judge_enabled():
	state = _make_state()
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = 1
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 5
	labels = {"10": ["ai:implementing"], "11": ["ai:merged"]}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=8 * 60 * 60,
		max_recoveries=5,
		stall_judge_trigger_count=2,
		enable_stall_judge=True,
	)

	assert len(stalls) == 1
	assert stalls[0]["recovery_action"] == "skip"


def test_cmd_check_stalls_forwards_stall_judge_flags_to_detect_stalls_with_trigger_override():
	captured: dict[str, object] = {}
	original_detect_stalls = orchestrate_lib.detect_stalls

	def _fake_detect_stalls(
		state: dict,
		issue_labels: dict[str, list[str]],
		threshold_minutes: int,
		now_ts: int,
		max_recoveries: int = 5,
		phase_thresholds: dict[str, int] | None = None,
		stall_judge_trigger_count: int = 2,
		enable_stall_judge: bool = True,
		allow_human_terminalization: bool = False,
	) -> list[dict[str, object]]:
		captured["state"] = state
		captured["issue_labels"] = issue_labels
		captured["threshold_minutes"] = threshold_minutes
		captured["now_ts"] = now_ts
		captured["max_recoveries"] = max_recoveries
		captured["phase_thresholds"] = phase_thresholds
		captured["stall_judge_trigger_count"] = stall_judge_trigger_count
		captured["enable_stall_judge"] = enable_stall_judge
		captured["allow_human_terminalization"] = allow_human_terminalization
		return []

	orchestrate_lib.detect_stalls = _fake_detect_stalls
	try:
		state = _make_state()
		labels = {"10": ["ai:planning"], "11": ["ai:planning"]}
		_ = _run_check_stalls(
			state,
			labels,
			threshold_minutes=45,
			now_ts=777,
			max_recoveries=6,
			phase_thresholds_json='{"ai:planning": 90}',
			stall_judge_trigger_count=3,
			enable_stall_judge="true",
			allow_human_terminalization="true",
		)
	finally:
		orchestrate_lib.detect_stalls = original_detect_stalls

	assert captured["threshold_minutes"] == 45
	assert captured["now_ts"] == 777
	assert captured["max_recoveries"] == 6
	assert captured["phase_thresholds"] == {"ai:planning": 90}
	assert captured["stall_judge_trigger_count"] == 3
	assert captured["enable_stall_judge"] is True
	assert captured["allow_human_terminalization"] is True


def test_detect_stalls_returns_run_stall_judge_at_trigger():
	now_ts = 5000
	waves = [
		{
			"wave": 1,
			"issues": [
				{
					"id": "issue-1",
					"github_issue": 10,
					"status": "pending",
					"status_since_ts": 1000,
					"stall_recovery_count": 2,
				}
			],
		}
	]
	state = _make_state(waves=waves, current_wave=1)
	labels = {"10": ["ai:planning"]}
	result = orchestrate_lib.detect_stalls(
		state,
		labels,
		threshold_minutes=1,
		now_ts=now_ts,
		max_recoveries=5,
		stall_judge_trigger_count=2,
		enable_stall_judge=True,
	)
	assert len(result) == 1
	assert result[0]["recovery_action"] == "run_stall_judge"


def test_detect_stalls_still_skips_at_or_above_max_recoveries():
	now_ts = 5000
	waves = [
		{
			"wave": 1,
			"issues": [
				{
					"id": "issue-1",
					"github_issue": 10,
					"status": "pending",
					"status_since_ts": 1000,
					"stall_recovery_count": 5,
				}
			],
		}
	]
	state = _make_state(waves=waves, current_wave=1)
	labels = {"10": ["ai:planning"]}
	result = orchestrate_lib.detect_stalls(
		state,
		labels,
		threshold_minutes=1,
		now_ts=now_ts,
		max_recoveries=5,
		stall_judge_trigger_count=2,
		enable_stall_judge=True,
	)
	assert len(result) == 1
	assert result[0]["recovery_action"] == "skip"


def test_detect_stalls_uses_ladder_when_stall_judge_disabled_for_planning_phase_low_trigger():
	now_ts = 5000
	waves = [
		{
			"wave": 1,
			"issues": [
				{
					"id": "issue-1",
					"github_issue": 10,
					"status": "pending",
					"status_since_ts": 1000,
					"stall_recovery_count": 1,
				}
			],
		}
	]
	state = _make_state(waves=waves, current_wave=1)
	labels = {"10": ["ai:planning"]}
	result = orchestrate_lib.detect_stalls(
		state,
		labels,
		threshold_minutes=1,
		now_ts=now_ts,
		max_recoveries=5,
		stall_judge_trigger_count=0,
		enable_stall_judge=False,
	)
	assert len(result) == 1
	assert result[0]["recovery_action"] == orchestrate_lib.STALL_RECOVERY_ACTIONS["ai:planning"][1]


def test_cmd_check_stalls_forwards_stall_judge_flags_to_detect_stalls_when_explicitly_enabled():
	captured: dict[str, object] = {}
	original_detect_stalls = orchestrate_lib.detect_stalls

	def _fake_detect_stalls(
		state: dict,
		issue_labels: dict[str, list[str]],
		threshold_minutes: int,
		now_ts: int,
		max_recoveries: int = 5,
		phase_thresholds: dict[str, int] | None = None,
		stall_judge_trigger_count: int = 0,
		enable_stall_judge: bool = False,
		allow_human_terminalization: bool = False,
	) -> list[dict[str, object]]:
		captured["state"] = state
		captured["issue_labels"] = issue_labels
		captured["threshold_minutes"] = threshold_minutes
		captured["now_ts"] = now_ts
		captured["max_recoveries"] = max_recoveries
		captured["phase_thresholds"] = phase_thresholds
		captured["stall_judge_trigger_count"] = stall_judge_trigger_count
		captured["enable_stall_judge"] = enable_stall_judge
		captured["allow_human_terminalization"] = allow_human_terminalization
		return []

	orchestrate_lib.detect_stalls = _fake_detect_stalls
	try:
		state = _make_state()
		labels = {"10": ["ai:planning"], "11": ["ai:planning"]}
		_ = _run_check_stalls(
			state,
			labels,
			threshold_minutes=45,
			now_ts=777,
			max_recoveries=6,
			phase_thresholds_json='{"ai:planning": 90}',
			stall_judge_trigger_count=3,
			enable_stall_judge="true",
			allow_human_terminalization="false",
		)
	finally:
		orchestrate_lib.detect_stalls = original_detect_stalls

	assert captured["threshold_minutes"] == 45
	assert captured["now_ts"] == 777
	assert captured["max_recoveries"] == 6
	assert captured["phase_thresholds"] == {"ai:planning": 90}
	assert captured["stall_judge_trigger_count"] == 3
	assert captured["enable_stall_judge"] is True
	assert captured["allow_human_terminalization"] is False


# ---------------------------------------------------------------------------
# Tests: state schema
# ---------------------------------------------------------------------------

def test_build_tracking_state_has_review_blocked_retries():
	data = _make_decomposition()
	waves = orchestrate_lib.compute_waves(data)
	issue_map = {"issue-1": 10, "issue-2": 11}
	state = orchestrate_lib.build_tracking_state(data, waves, issue_map)
	assert "review_blocked_retries" in state
	assert isinstance(state["review_blocked_retries"], dict)
	assert len(state["review_blocked_retries"]) == 0


def test_build_tracking_state_schema():
	data = _make_decomposition()
	waves = orchestrate_lib.compute_waves(data)
	issue_map = {"issue-1": 10}
	state = orchestrate_lib.build_tracking_state(data, waves, issue_map, integration_branch="orchestrator/project-42")
	assert state["schema_version"] == "orchestrate_state.v1"
	assert state["total_issues"] == 2
	assert state["total_waves"] == 1
	assert state["current_wave"] == 1
	assert state["judge_cycle"] == 0
	assert state["recovery_attempted"] is False
	assert state["status"] == "in_progress"
	assert state["integration_branch"] == "orchestrator/project-42"
	assert state["final_merge_strategy"] == "squash"
	assert state["final_merge_pr"] is None
	assert state["final_merge_status"] == "pending"
	# issue-2 should be in pending_issue_defs (not in issue_map)
	assert "issue-2" in state["pending_issue_defs"]


def test_build_tracking_issue_body_includes_integration_branch():
	data = _make_decomposition()
	waves = orchestrate_lib.compute_waves(data)
	body = orchestrate_lib.build_tracking_issue_body(
		data,
		waves,
		integration_branch="orchestrator/project-55",
	)
	assert "**Integration branch:** `orchestrator/project-55`" in body


def test_parse_tracking_body_extracts_integration_branch():
	body = """## Project: Test Project

Summary

---

**Total issues:** 1 | **Waves:** 1
**Integration branch:** `orchestrator/project-66`

### Wave 1

- [ ] **issue-1**: First task (priority 1)

---
"""
	parsed = orchestrate_lib.parse_tracking_body(body)
	assert parsed["integration_branch"] == "orchestrator/project-66"


def test_rebuild_tracking_state_preserves_integration_defaults():
	body = """## Project: Test Project

Summary

---

**Total issues:** 1 | **Waves:** 1
**Integration branch:** `orchestrator/project-77`

### Wave 1

- [ ] **issue-1**: First task (priority 1)

---
"""
	state = orchestrate_lib.rebuild_tracking_state(body, {"issue-1": 10}, tracking_issue=123)
	assert state["integration_branch"] == "orchestrator/project-77"
	assert state["final_merge_strategy"] == "squash"
	assert state["final_merge_pr"] is None
	assert state["final_merge_status"] == "pending"


# ---------------------------------------------------------------------------
# Tests: label priority (review-blocked takes precedence over in_progress)
# ---------------------------------------------------------------------------

def test_review_blocked_label_priority_over_other_labels():
	"""ai:review-blocked should be detected even if other non-phase labels are present."""
	state = _make_state()
	labels = {"10": ["bug", "ai:review-blocked", "enhancement"], "11": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["any_review_blocked"] is True
	issues_by_gh = {i["github_issue"]: i for i in result["issues"]}
	assert issues_by_gh[10]["status"] == "review-blocked"


def test_reconcile_wave_status_precedence_pr_then_label_then_issue_state():
	issue = {"id": "task-1", "github_issue": 10, "status": "pending"}
	status, source = orchestrate_lib.reconcile_wave_issue_status(
		issue=issue,
		labels=["ai:planning"],
		issue_state="closed",
		pr_state="closed",
		pr_merged=True,
	)
	assert status == "merged"
	assert source == "linked_pr_merged"

	status, source = orchestrate_lib.reconcile_wave_issue_status(
		issue=issue,
		labels=["ai:merged"],
		issue_state="closed",
		pr_state="closed",
		pr_merged=False,
	)
	assert status == "merged"
	assert source == "label_ai_merged"

	status, source = orchestrate_lib.reconcile_wave_issue_status(
		issue=issue,
		labels=[],
		issue_state="closed",
		pr_state="closed",
		pr_merged=False,
	)
	assert status == "closed"
	assert source == "issue_closed"


def test_reconcile_wave_status_terminal_non_regression():
	issue_merged = {"id": "task-1", "github_issue": 10, "status": "merged"}
	status_merged, source_merged = orchestrate_lib.reconcile_wave_issue_status(
		issue=issue_merged,
		labels=["ai:implementing"],
		issue_state="open",
		pr_state="open",
		pr_merged=False,
	)
	assert status_merged == "merged"
	assert source_merged == "stored_terminal"

	issue_closed = {"id": "task-2", "github_issue": 11, "status": "closed"}
	status_closed, source_closed = orchestrate_lib.reconcile_wave_issue_status(
		issue=issue_closed,
		labels=["ai:planning"],
		issue_state="open",
		pr_state="open",
		pr_merged=False,
	)
	assert status_closed == "closed"
	assert source_closed == "stored_terminal"


def test_detect_stalls_skips_needs_human_phase():
	state = _make_state()
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["status_since_ts"] = 1
	issue["last_seen_phase"] = "ai:needs-human"
	issue["stall_recovery_count"] = 2

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels={"10": ["ai:planning", "ai:needs-human"], "11": []},
		threshold_minutes=1,
		now_ts=10_000,
	)

	assert stalls == []


def test_label_contract_matches_helper_catalog_and_phase_priority():
	contract_path = Path(__file__).resolve().parent.parent / ".github" / "ai" / "label_contract.v1.json"
	helper_path = Path(__file__).resolve().parent.parent / "scripts" / "label_helpers.sh"
	contract = json.loads(contract_path.read_text(encoding="utf-8"))
	contract_labels = set(contract["labels"].keys())

	helper_body = helper_path.read_text(encoding="utf-8")
	colors_block_match = re.search(
		r"declare -A _AI_LABEL_COLORS=\((.*?)\n\)",
		helper_body,
		flags=re.S,
	)
	assert colors_block_match, "Could not parse _AI_LABEL_COLORS from label_helpers.sh"
	helper_labels = set(re.findall(r'\["([^"]+)"\]=', colors_block_match.group(1)))
	assert helper_labels == contract_labels, (
		f"label_helpers catalog drift detected.\n"
		f"Missing in helper: {sorted(contract_labels - helper_labels)}\n"
		f"Extra in helper: {sorted(helper_labels - contract_labels)}"
	)

	phase_labels = {
		member
		for group in contract.get("phase_groups", [])
		for member in group.get("members", [])
	}
	priority_labels = set(orchestrate_lib.PHASE_LABELS_PRIORITY)
	missing_priority = sorted(phase_labels - priority_labels)
	assert not missing_priority, (
		f"PHASE_LABELS_PRIORITY missing contract phase labels: {missing_priority}"
	)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
	test_funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
	passed = 0
	failed = 0
	for func in test_funcs:
		name = func.__name__
		try:
			func()
			print(f"  PASS  {name}")
			passed += 1
		except Exception as e:
			print(f"  FAIL  {name}: {e}")
			failed += 1

	print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
	return 1 if failed > 0 else 0


if __name__ == "__main__":
	raise SystemExit(main())
