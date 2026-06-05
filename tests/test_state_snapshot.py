#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_STATE_SNAPSHOT_PATH = REPO_ROOT / "scripts" / "build_state_snapshot.py"
POLLER_SCRIPT_PATH = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "orchestrate_poll.yml"
if str(REPO_ROOT) not in sys.path:
	sys.path.insert(0, str(REPO_ROOT))


def _load_module(name: str, path: Path):
	spec = importlib.util.spec_from_file_location(name, path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


build_state_snapshot = _load_module("build_state_snapshot", BUILD_STATE_SNAPSHOT_PATH)
ai_memory_lib = sys.modules["ai_memory_lib"]


@contextlib.contextmanager
def _patched_module_attrs(module, **replacements):
	originals = {name: getattr(module, name) for name in replacements}
	try:
		for name, value in replacements.items():
			setattr(module, name, value)
		yield
	finally:
		for name, value in originals.items():
			setattr(module, name, value)


def _create_memory_root(tmp_path: Path) -> Path:
	memory_root = tmp_path / "ai-memory"
	(memory_root / "schemas").mkdir(parents=True, exist_ok=True)
	for schema_name in ("run_ledger_entry.v1.json", "state_snapshot.v1.json"):
		shutil.copy2(REPO_ROOT / "ai-memory" / "schemas" / schema_name, memory_root / "schemas" / schema_name)
	return memory_root


def _write_run_ledger_entries(memory_root: Path, run_id: int, entries: list[dict[str, object]]) -> None:
	ledger_path = memory_root / "runs" / str(run_id) / "ledger" / "events.jsonl"
	ledger_path.parent.mkdir(parents=True, exist_ok=True)
	ledger_path.write_text(
		"".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries),
		encoding="utf-8",
	)


def _run_substate_entry(*, run_id: int, substate: str, tokens: dict[str, int] | None) -> dict[str, object]:
	metadata: dict[str, object] = {
		"attempt": 1,
		"mode": "implement",
		"phase": "implement",
		"run_substate": substate,
	}
	if tokens is not None:
		metadata["tokens"] = tokens
	return {
		"entry_id": f"entry-{run_id}-{substate}",
		"schema_version": "run_ledger_entry.v1",
		"run_id": str(run_id),
		"workflow": "implement",
		"issue_number": 10,
		"pr_number": None,
		"event_type": "run_substate",
		"status": "ok",
		"message": f"Run substate {substate}",
		"actor": "codex-bot",
		"metadata": metadata,
		"timestamp": "2026-06-05T18:00:00Z",
	}


def _tracker_export(*, ledger_substates_enabled: bool = True, runtime_blocker_check_enabled: bool = True) -> dict[str, object]:
	return {
		"number": 3142,
		"title": "Tracking issue #3142",
		"state": {
			"status": "in_progress",
			"current_wave": 1,
			"total_waves": 1,
			"waves": [
				{
					"wave": 1,
					"issues": [
						{"id": "issue-10", "github_issue": 10, "status": "in_progress"},
						{"id": "issue-11", "github_issue": None, "status": "pending"},
					],
				}
			],
			"dependency_edges": [{"from": "issue-10", "to": "issue-11"}],
			"issue_number_map": {"issue-10": 10},
			"pending_issue_defs": {
				"issue-11": {"title": "Blocked issue", "body": "body", "priority": 2}
			},
		},
		"wave_status": {
			"wave": 1,
			"wave_complete": False,
			"project_complete": False,
			"any_failed": False,
			"issues": [
				{"id": "issue-10", "github_issue": 10, "status": "in_progress"},
				{"id": "issue-11", "github_issue": None, "status": "not_created"},
			],
		},
		"labels_json": {"10": ["ai:implementing"]},
		"issue_states_json": {"10": "open"},
		"pr_states_json": {"10": {"state": "unknown", "merged": False}},
		"candidate_details_json": {
			"10": {"state": "open", "labels": ["ai:implementing"], "linked_pr": None}
		},
		"runtime_blocker_check_enabled": runtime_blocker_check_enabled,
		"ledger_substates_enabled": ledger_substates_enabled,
	}


def _tracking_issues_payload() -> list[dict[str, object]]:
	return [{"number": 3142, "title": "Tracking issue #3142"}]


def _actions_runs_payload() -> dict[str, object]:
	return {
		"workflow_runs": [
			{
				"id": 501,
				"status": "in_progress",
				"head_branch": "ai/issue-10",
				"name": "AI Implement",
				"html_url": "https://example.com/runs/501",
				"run_started_at": "2026-06-05T18:01:00Z",
			}
		]
	}


def _build_snapshot(*, memory_root: Path, tracker_export: dict[str, object] | None) -> dict[str, object]:
	tracker_exports = [] if tracker_export is None else [tracker_export]
	return build_state_snapshot.build_state_snapshot(
		repo_root=REPO_ROOT,
		tracker_exports=tracker_exports,
		tracking_issues_payload=_tracking_issues_payload(),
		actions_runs_payload=_actions_runs_payload(),
		tick_at="2026-06-05T18:30:00Z",
		schema_memory_root=memory_root,
		ledger_memory_root=memory_root,
	)


def _extract_function_body(script_text: str, function_name: str) -> str:
	match = re.search(rf"^{function_name}\(\) \{{\n(?P<body>.*?)^\}}\n", script_text, re.MULTILINE | re.DOTALL)
	assert match is not None, function_name
	return match.group("body")


def _extract_workflow_step_if(step_name: str) -> str:
	lines = WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
	for idx, line in enumerate(lines):
		if line.strip() != f"- name: {step_name}":
			continue
		for candidate in lines[idx + 1:idx + 6]:
			candidate = candidate.strip()
			if candidate.startswith("if:"):
				return candidate
		raise AssertionError(f"missing if condition for step {step_name}")
	raise AssertionError(f"missing workflow step {step_name}")


def test_snapshot_builder_generates_schema_valid_payload_with_enrichment_and_deferred_entries() -> None:
	with tempfile.TemporaryDirectory() as tmp_dir:
		tmp_path = Path(tmp_dir)
		memory_root = _create_memory_root(tmp_path)
		_write_run_ledger_entries(
			memory_root,
			501,
			[
				_run_substate_entry(run_id=501, substate="PreparingWorkspace", tokens=None),
				_run_substate_entry(run_id=501, substate="StreamingTurn", tokens={"input": 8, "output": 4, "total": 12}),
			],
		)

		snapshot = _build_snapshot(memory_root=memory_root, tracker_export=_tracker_export())
		ai_memory_lib.validate_state_snapshot_payload(snapshot, memory_root)

		assert snapshot["tracking_issues"] == [
			{
				"number": 3142,
				"title": "Tracking issue #3142",
				"status": "in-progress",
				"current_wave": 1,
				"total_waves": 1,
				"wave_complete": False,
				"project_complete": False,
				"issue_counts": {
					"merged": 0,
					"closed": 0,
					"skipped": 0,
					"in_progress": 1,
					"review_blocked": 0,
					"ready_to_merge": 0,
					"done": 0,
					"implementation_failed": 0,
					"not_created": 1,
				},
			}
		]

		running_entry = snapshot["running"][0]
		assert running_entry["tracking_issue"] == 3142
		assert running_entry["wave"] == 1
		assert running_entry["local_id"] == "issue-10"
		assert running_entry["github_issue"] == 10
		assert running_entry["run_id"] == 501
		assert running_entry["workflow"] == "AI Implement"
		assert running_entry["phase"] == "ai:implementing"
		assert running_entry["substate"] == "StreamingTurn"
		assert running_entry["tokens"] == {"input": 8, "output": 4, "total": 12}

		assert snapshot["deferred"] == [
			{
				"tracking_issue": 3142,
				"wave": 1,
				"local_id": "issue-11",
				"reason": "blocked_by_dependency",
				"metadata_present": True,
				"blocker_count": 1,
				"blockers": [
					{
						"local_id": "issue-10",
						"github_issue": 10,
						"terminal": False,
						"status": "in_progress",
						"source": "default_in_progress",
					}
				],
			}
		]

		assert snapshot["totals"] == {
			"tracking_issues": 1,
			"running": 1,
			"deferred": 1,
			"tokens": {"input": 8, "output": 4, "total": 12},
		}


def test_snapshot_builder_omits_substate_and_tokens_when_ledger_enrichment_is_disabled() -> None:
	with tempfile.TemporaryDirectory() as tmp_dir:
		tmp_path = Path(tmp_dir)
		memory_root = _create_memory_root(tmp_path)
		_write_run_ledger_entries(
			memory_root,
			501,
			[_run_substate_entry(run_id=501, substate="StreamingTurn", tokens={"input": 5, "output": 2, "total": 7})],
		)

		snapshot = _build_snapshot(
			memory_root=memory_root,
			tracker_export=_tracker_export(ledger_substates_enabled=False),
		)
		ai_memory_lib.validate_state_snapshot_payload(snapshot, memory_root)

		running_entry = snapshot["running"][0]
		assert "substate" not in running_entry
		assert "tokens" not in running_entry
		assert snapshot["totals"]["tokens"] == {"input": 0, "output": 0, "total": 0}


def test_snapshot_builder_emits_placeholder_tracking_summary_for_empty_tick() -> None:
	with tempfile.TemporaryDirectory() as tmp_dir:
		tmp_path = Path(tmp_dir)
		memory_root = _create_memory_root(tmp_path)

		snapshot = build_state_snapshot.build_state_snapshot(
			repo_root=REPO_ROOT,
			tracker_exports=[],
			tracking_issues_payload=_tracking_issues_payload(),
			actions_runs_payload={"workflow_runs": []},
			tick_at="2026-06-05T18:30:00Z",
			schema_memory_root=memory_root,
			ledger_memory_root=memory_root,
		)
		ai_memory_lib.validate_state_snapshot_payload(snapshot, memory_root)

		assert snapshot == {
			"schema_version": "state_snapshot.v1",
			"tick_at": "2026-06-05T18:30:00Z",
			"tracking_issues": [
				{
					"number": 3142,
					"title": "Tracking issue #3142",
					"status": "in-progress",
					"current_wave": 0,
					"total_waves": 0,
					"wave_complete": False,
					"project_complete": False,
					"issue_counts": {
						"merged": 0,
						"closed": 0,
						"skipped": 0,
						"in_progress": 0,
						"review_blocked": 0,
						"ready_to_merge": 0,
						"done": 0,
						"implementation_failed": 0,
						"not_created": 0,
					},
				}
			],
			"running": [],
			"deferred": [],
			"totals": {
				"tracking_issues": 1,
				"running": 0,
				"deferred": 0,
				"tokens": {"input": 0, "output": 0, "total": 0},
			},
		}


def test_main_uses_explicit_memory_root_without_branch_clone() -> None:
	with tempfile.TemporaryDirectory() as tmp_dir:
		tmp_path = Path(tmp_dir)
		memory_root = _create_memory_root(tmp_path)
		tracking_issues_file = tmp_path / "tracking_issues.json"
		trackers_dir = tmp_path / "trackers"
		actions_runs_file = tmp_path / "actions_runs.json"
		output_file = tmp_path / "state.json"

		trackers_dir.mkdir(parents=True, exist_ok=True)
		tracking_issues_file.write_text(json.dumps(_tracking_issues_payload()), encoding="utf-8")
		(trackers_dir / "tracking_3142.json").write_text(json.dumps(_tracker_export()), encoding="utf-8")
		actions_runs_file.write_text(json.dumps(_actions_runs_payload()), encoding="utf-8")

		def _unexpected_branch_read(*_args, **_kwargs):
			raise AssertionError("read_memory_root_from_branch should not run when --memory-root-dir is supplied")

		with _patched_module_attrs(build_state_snapshot, read_memory_root_from_branch=_unexpected_branch_read):
			rc = build_state_snapshot.main(
				[
					"--repo-root",
					str(REPO_ROOT),
					"--tracking-issues-file",
					str(tracking_issues_file),
					"--trackers-dir",
					str(trackers_dir),
					"--actions-runs-file",
					str(actions_runs_file),
					"--output-file",
					str(output_file),
					"--memory-root-dir",
					str(memory_root),
				]
			)

		assert rc == 0
		payload = json.loads(output_file.read_text(encoding="utf-8"))
		ai_memory_lib.validate_state_snapshot_payload(payload, memory_root)


def test_poller_snapshot_exports_reuse_cached_data_without_new_api_calls() -> None:
	script_text = POLLER_SCRIPT_PATH.read_text(encoding="utf-8")
	actions_body = _extract_function_body(script_text, "write_state_snapshot_actions_runs_export")
	tracker_body = _extract_function_body(script_text, "write_state_snapshot_tracker_export")

	assert "_ACTIONS_RUNS_BLOB_CACHE" in actions_body
	assert "state_snapshot_actions_runs.json" in actions_body
	for forbidden in ("gh ", "gh_retry", "_safe_gh_jq", "_load_actions_runs_cached"):
		assert forbidden not in actions_body, forbidden

	for required in (
		"STATE_FILE",
		"WAVE_STATUS",
		"LABELS_JSON",
		"ISSUE_STATES_JSON",
		"PR_STATES_JSON",
		"_current_wave_details_json",
		"state_snapshot_trackers",
	):
		assert required in tracker_body, required
	for forbidden in (
		"gh ",
		"gh_retry",
		"_safe_gh_jq",
		"_fetch_candidate_issue_details_graphql",
		"_fetch_issue_labels_batch_graphql",
		"_fetch_pr_json",
		"get_issue_labels_json",
	):
		assert forbidden not in tracker_body, forbidden

	assert 'write_state_snapshot_actions_runs_export || true' in script_text
	assert 'write_state_snapshot_tracker_export "${TRACKING_NUM}" "${TRACKING_TITLE}" || true' in script_text


def test_snapshot_workflow_artifact_steps_are_not_gated_on_has_work() -> None:
	for step_name in ("Build state snapshot", "Upload state snapshot artifact"):
		condition = _extract_workflow_step_if(step_name)
		assert "always()" in condition
		assert "env.STATE_SNAPSHOT_ARTIFACT_ENABLED != 'false'" in condition
		assert "steps.find_tracking.outputs.has_work == 'true'" not in condition

	condition = _extract_workflow_step_if("Publish state snapshot branch")
	assert "always()" in condition
	assert "env.STATE_SNAPSHOT_ARTIFACT_ENABLED != 'false'" in condition
	assert "env.STATE_SNAPSHOT_BRANCH_ENABLED == 'true'" in condition
	assert "steps.find_tracking.outputs.has_work == 'true'" not in condition


def main() -> int:
	test_funcs = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
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
	return 1 if failed else 0


if __name__ == "__main__":
	raise SystemExit(main())
