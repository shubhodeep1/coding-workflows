#!/usr/bin/env python3
"""Unit tests for orchestrate_lib.py — wave computation, status checks, and state schema."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# Add scripts/ to path so we can import orchestrate_lib
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import orchestrate_lib


REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "integration_ref_resolver"


def _iter_integration_fixtures() -> list[dict]:
	fixtures: list[dict] = []
	for path in sorted(INTEGRATION_FIXTURE_DIR.glob("*.json")):
		fixtures.append(json.loads(path.read_text(encoding="utf-8")))
	return fixtures


def _fixture_path_by_name(name: str) -> Path:
	return INTEGRATION_FIXTURE_DIR / f"{name}.json"


def _write_mock_gh(bin_dir: Path) -> None:
	script = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys
from urllib.parse import unquote


def _load_fixture() -> dict:
	path = pathlib.Path(os.environ['GH_FIXTURE_FILE'])
	return json.loads(path.read_text(encoding='utf-8'))


def _parse_issue(endpoint: str):
	parts = endpoint.split('/')
	if len(parts) < 5:
		return None
	if parts[0] != 'repos' or parts[3] != 'issues':
		return None
	try:
		return int(parts[4])
	except ValueError:
		return None


def main() -> int:
	args = sys.argv[1:]
	if len(args) < 2 or args[0] != 'api':
		print('mock gh only supports gh api', file=sys.stderr)
		return 2
	fixture = _load_fixture()
	endpoint = args[1]
	if endpoint.startswith('repos/') and '/issues/' in endpoint:
		issue_num = _parse_issue(endpoint)
		if issue_num is None:
			print('invalid issue endpoint', file=sys.stderr)
			return 2
		body = fixture.get('issues', {}).get(str(issue_num), {}).get('body', '')
		if '--jq' in args:
			jq_expr = args[args.index('--jq') + 1]
			if jq_expr == '.body // ""':
				print(body)
				return 0
		print(json.dumps({'number': issue_num, 'body': body}))
		return 0
	if endpoint.startswith('repos/') and '/git/ref/heads/' in endpoint:
		ref = unquote(endpoint.split('/git/ref/heads/', 1)[1])
		exists = bool(fixture.get('branch_exists', {}).get(ref, False))
		if exists:
			print(json.dumps({'ref': f'refs/heads/{ref}'}))
			return 0
		print('gh: Not Found (HTTP 404)', file=sys.stderr)
		return 1
	print(f'unsupported endpoint: {endpoint}', file=sys.stderr)
	return 2


if __name__ == '__main__':
	raise SystemExit(main())
"""
	gh_path = bin_dir / "gh"
	gh_path.write_text(script, encoding="utf-8")
	gh_path.chmod(0o755)


def _run_bash_resolver(fixture_path: Path, bin_dir: Path) -> tuple[int, str, str]:
	env = os.environ.copy()
	env.update(
		{
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"GH_TOKEN": "test-token",
			"GH_FIXTURE_FILE": str(fixture_path),
			"REPO": "owner/repo",
			"ISSUE": "101",
		}
	)
	proc = subprocess.run(
		["bash", str(REPO_ROOT / "scripts" / "resolve_integration_ref.sh")],
		check=False,
		capture_output=True,
		text=True,
		env=env,
		cwd=str(REPO_ROOT),
	)
	return proc.returncode, proc.stdout.strip(), proc.stderr


def _run_python_resolver(fixture_path: Path, bin_dir: Path) -> tuple[int, str, str]:
	env = os.environ.copy()
	env.update(
		{
			"PATH": f"{bin_dir}:{env.get('PATH', '')}",
			"GH_TOKEN": "test-token",
			"GH_FIXTURE_FILE": str(fixture_path),
			"REPO": "owner/repo",
		}
	)
	proc = subprocess.run(
		[
			"python3",
			str(REPO_ROOT / "scripts" / "orchestrate_lib.py"),
			"--print-integration-ref",
			"101",
		],
		check=False,
		capture_output=True,
		text=True,
		env=env,
		cwd=str(REPO_ROOT),
	)
	return proc.returncode, proc.stdout.strip(), proc.stderr


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
	max_recoveries_by_phase_json: str | None = None,
	stall_judge_trigger_count: int = 2,
	enable_stall_judge: str = "false",
	enable_stall_human_terminalization: str = "false",
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
						"max_recoveries_by_phase_json": max_recoveries_by_phase_json,
					"now_ts": str(now_ts),
					"stall_judge_trigger_count": str(stall_judge_trigger_count),
					"enable_stall_judge": enable_stall_judge,
					"enable_stall_human_terminalization": enable_stall_human_terminalization,
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


def test_check_wave_status_final_wave_one_closed_rest_merged_is_complete_but_failed():
	"""Contract underpinning the validate-dispatch deadlock fix
	(hylifegroup.com#3): a final wave whose issues are all ``ai:merged``
	EXCEPT one legitimately closed without a merged PR (``ai:closed`` — e.g. a
	judge-fix-up that needed no code change) yields ``wave_complete=true`` AND
	``any_failed=true`` simultaneously. ``"closed"`` is in the
	merged/closed/skipped set so it keeps ``all_merged`` (hence
	``wave_complete``) True, while still flipping ``any_failed`` True. This is
	the only failed-wave shape that should still allow validate dispatch.

	The orchestrator MUST therefore NOT gate validate-dispatch on
	``any_failed`` (see ``dispatch_validation_if_needed`` in
	``scripts/orchestrate_poll_process.sh``): doing so deferred dispatch every
	poll cycle and wedged the project in ``ai:validating`` indefinitely. This
	test pins the value pair the dispatch gate now relies on."""
	state = _make_state()
	labels = {"10": ["ai:closed"], "11": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is True
	assert result["any_failed"] is True
	assert result["validation_dispatch_safe_despite_failures"] is True
	# Single (final) wave with no separate integration branch -> the project
	# is genuinely complete; the closed wave issue must not change that.
	assert result["project_complete"] is True
	issues_by_gh = {i["github_issue"]: i for i in result["issues"]}
	assert issues_by_gh[10]["status"] == "closed"
	assert issues_by_gh[11]["status"] == "merged"


def test_check_wave_status_failure_phase_counts_as_failed():
	state = _make_state()
	labels = {"10": ["ai:plan-failed"], "11": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["any_failed"] is True
	assert result["validation_dispatch_safe_despite_failures"] is False
	issues_by_gh = {i["github_issue"]: i for i in result["issues"]}
	assert issues_by_gh[10]["status"] == "closed"
	assert issues_by_gh[10]["decision_source"] == "label_terminal_phase"


def test_check_wave_status_closed_label_does_not_mask_failure_phase():
	state = _make_state()
	labels = {"10": ["ai:closed", "ai:plan-failed"], "11": ["ai:merged"]}
	result = _run_check_wave_status(state, labels)
	assert result["wave_complete"] is True
	assert result["any_failed"] is True
	assert result["validation_dispatch_safe_despite_failures"] is False
	issues_by_gh = {i["github_issue"]: i for i in result["issues"]}
	assert issues_by_gh[10]["status"] == "closed"
	assert issues_by_gh[10]["decision_source"] == "label_ai_closed"


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


def test_check_wave_status_unblocks_task_state_dependents_on_first_terminal_transition():
	import io
	from contextlib import redirect_stderr, redirect_stdout

	import task_state

	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root = task_state.REPO_ROOT
		previous_flag = os.environ.get("ORCH_TASK_FILES_ENABLED")
		task_state.REPO_ROOT = root
		os.environ["ORCH_TASK_FILES_ENABLED"] = "true"
		try:
			issue_one = {"id": "issue-1", "github_issue": 10, "status": "pending"}
			issue_two = {
				"id": "issue-2",
				"github_issue": 11,
				"status": "pending",
				"depends_on": ["issue-1"],
				"reissue_depends_on": [10, 999],
			}
			assert task_state.write_task(1, "issue-1", issue_one)
			assert task_state.write_task(1, "issue-2", issue_two)

			first_state = _make_state(
				waves=[{"wave": 1, "issues": [dict(issue_one), dict(issue_two)]}],
				current_wave=1,
			)
			with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
				json.dump(first_state, handle)
				first_state_path = handle.name

			try:
				first_stdout = io.StringIO()
				first_stderr = io.StringIO()
				with redirect_stdout(first_stdout), redirect_stderr(first_stderr):
					assert orchestrate_lib.cmd_check_wave_status(
						type("Args", (), {
							"state_file": first_state_path,
							"labels_json": json.dumps({"10": ["ai:merged"], "11": []}),
						})()
					) == 0
			finally:
				os.unlink(first_state_path)

			assert json.loads(first_stdout.getvalue())["wave_complete"] is False
			assert task_state.read_task(1, "issue-2") == {
				"depends_on": [],
				"github_issue": 11,
				"id": "issue-2",
				"reissue_depends_on": [999],
				"schema_version": "task_state.v1.json",
				"status": "pending",
			}
			assert "TASK_STATE_UNBLOCK 1 issue-1 1" in first_stderr.getvalue()

			second_state = _make_state(
				waves=[{"wave": 1, "issues": [{**issue_one, "status": "merged"}, dict(issue_two)]}],
				current_wave=1,
			)
			with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
				json.dump(second_state, handle)
				second_state_path = handle.name

			try:
				second_stdout = io.StringIO()
				second_stderr = io.StringIO()
				with redirect_stdout(second_stdout), redirect_stderr(second_stderr):
					assert orchestrate_lib.cmd_check_wave_status(
						type("Args", (), {
							"state_file": second_state_path,
							"labels_json": json.dumps({"10": ["ai:merged"], "11": []}),
						})()
					) == 0
			finally:
				os.unlink(second_state_path)

			assert json.loads(second_stdout.getvalue())["wave_complete"] is False
			assert "TASK_STATE_UNBLOCK" not in second_stderr.getvalue()
		finally:
			task_state.REPO_ROOT = previous_root
			if previous_flag is None:
				os.environ.pop("ORCH_TASK_FILES_ENABLED", None)
			else:
				os.environ["ORCH_TASK_FILES_ENABLED"] = previous_flag

def test_check_wave_status_does_not_unblock_task_state_dependents_for_not_created_transition():
	import io
	from contextlib import redirect_stderr, redirect_stdout

	import task_state

	with tempfile.TemporaryDirectory() as td:
		root = Path(td)
		previous_root = task_state.REPO_ROOT
		previous_flag = os.environ.get("ORCH_TASK_FILES_ENABLED")
		task_state.REPO_ROOT = root
		os.environ["ORCH_TASK_FILES_ENABLED"] = "true"
		try:
			issue_one = {"id": "issue-1", "github_issue": None, "status": "pending"}
			issue_two = {
				"id": "issue-2",
				"github_issue": 11,
				"status": "pending",
				"depends_on": ["issue-1"],
				"reissue_depends_on": [999],
			}
			assert task_state.write_task(1, "issue-1", issue_one)
			assert task_state.write_task(1, "issue-2", issue_two)

			state = _make_state(
				waves=[{"wave": 1, "issues": [dict(issue_one), dict(issue_two)]}],
				current_wave=1,
			)
			with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
				json.dump(state, handle)
				state_path = handle.name

			try:
				stdout_buffer = io.StringIO()
				stderr_buffer = io.StringIO()
				with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
					assert orchestrate_lib.cmd_check_wave_status(
						type("Args", (), {
							"state_file": state_path,
							"labels_json": json.dumps({"11": []}),
						})()
					) == 0
			finally:
				os.unlink(state_path)

			assert json.loads(stdout_buffer.getvalue())["wave_complete"] is False
			assert task_state.read_task(1, "issue-2") == {
				"depends_on": ["issue-1"],
				"github_issue": 11,
				"id": "issue-2",
				"reissue_depends_on": [999],
				"schema_version": "task_state.v1.json",
				"status": "pending",
			}
			assert "TASK_STATE_UNBLOCK" not in stderr_buffer.getvalue()
		finally:
			task_state.REPO_ROOT = previous_root
			if previous_flag is None:
				os.environ.pop("ORCH_TASK_FILES_ENABLED", None)
			else:
				os.environ["ORCH_TASK_FILES_ENABLED"] = previous_flag


def test_check_wave_status_task_state_failures_are_fail_open():
	import io
	from contextlib import redirect_stderr, redirect_stdout

	import task_state

	previous_flag = os.environ.get("ORCH_TASK_FILES_ENABLED")
	original_unblock_dependents = task_state.unblock_dependents
	os.environ["ORCH_TASK_FILES_ENABLED"] = "true"
	try:
		def _boom(_wave_id: object, _completed_issue_id: object) -> int:
			raise RuntimeError("boom")

		task_state.unblock_dependents = _boom
		state = _make_state()
		stdout_buffer = io.StringIO()
		stderr_buffer = io.StringIO()
		with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
			json.dump(state, handle)
			state_path = handle.name

		try:
			with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
				assert orchestrate_lib.cmd_check_wave_status(
					type("Args", (), {
						"state_file": state_path,
						"labels_json": json.dumps({"10": ["ai:merged"], "11": ["ai:merged"]}),
					})()
				) == 0
		finally:
			os.unlink(state_path)

		result = json.loads(stdout_buffer.getvalue())
		assert result["wave_complete"] is True
		assert result["project_complete"] is True
		assert "TASK_STATE_WRITE_FAIL issue-1 unblock_failed:boom" in stderr_buffer.getvalue()
	finally:
		task_state.unblock_dependents = original_unblock_dependents
		if previous_flag is None:
			os.environ.pop("ORCH_TASK_FILES_ENABLED", None)
		else:
			os.environ["ORCH_TASK_FILES_ENABLED"] = previous_flag

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


def _iso_z(epoch: int) -> str:
	"""Render a Unix epoch as an ISO 8601 UTC string with the trailing Z suffix.

	Mirrors GitHub's GraphQL ``pushedDate`` / ``committedDate`` shape so the
	re-anchor tests below exercise the same parser path as production input.
	"""
	import datetime as _dt
	return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ai_done_state_past_threshold(now_ts: int) -> tuple[dict, dict]:
	"""Build a wave with one ``ai:done`` issue whose ``status_since_ts`` sits
	200 minutes in the past, comfortably past the 120-minute ``ai:done``
	stall threshold so any failure to re-anchor still surfaces as a stall.
	"""
	state = _make_state()
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = now_ts - 200 * 60
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	labels = {"10": ["ai:done"], "11": ["ai:merged"]}
	return state, labels


def test_detect_stalls_ai_done_reanchors_to_fresh_head_pushed_at():
	"""Q2=A: an ``ai:done`` issue whose linked PR was pushed inside the
	stall window must NOT be flagged as stalled, even when
	``status_since_ts`` alone would put it well past the threshold.
	"""
	now_ts = 1_700_000_000
	state, labels = _ai_done_state_past_threshold(now_ts)

	# Push happened 30 minutes ago — well inside the 120-min ai:done window,
	# so the re-anchor must clamp the effective elapsed back below threshold.
	head_pushed_at = {"10": _iso_z(now_ts - 30 * 60)}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at=head_pushed_at,
	)

	assert stalls == [], (
		f"Fresh head-push should re-anchor ai:done out of stalled set; got {stalls!r}"
	)


def test_detect_stalls_ai_done_still_stalled_when_head_push_is_also_stale():
	"""Re-anchor must NOT mask a genuine stall: when ``headPushedAt`` itself
	is older than the stall threshold, the issue must still be flagged.
	"""
	now_ts = 1_700_000_000
	state, labels = _ai_done_state_past_threshold(now_ts)

	# Push happened 180 minutes ago — still past the 120-min threshold.
	head_pushed_at = {"10": _iso_z(now_ts - 180 * 60)}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at=head_pushed_at,
	)

	assert len(stalls) == 1, stalls
	assert stalls[0]["github_issue"] == 10
	# stall_duration_minutes reflects the re-anchored elapsed (now - max(status_since, push))
	assert stalls[0]["stall_duration_minutes"] == 180


def test_detect_stalls_reanchor_scope_excludes_non_ai_done_phases():
	"""Q2=A scopes the re-anchor to ``ai:done`` only.  Other phases keep
	the legacy ``status_since_ts``-only anchor, so a fresh head push on an
	``ai:implementing`` issue must NOT suppress the stall flag.
	"""
	now_ts = 1_700_000_000
	state = _make_state()
	state["waves"][0]["issues"][0]["status"] = "in_progress"
	state["waves"][0]["issues"][0]["status_since_ts"] = now_ts - 200 * 60
	state["waves"][0]["issues"][0]["stall_recovery_count"] = 0
	# ai:implementing has the same 120-min default threshold as ai:done, so the
	# only thing standing between "stalled" and "not stalled" is the re-anchor.
	labels = {"10": ["ai:implementing"], "11": ["ai:merged"]}

	head_pushed_at = {"10": _iso_z(now_ts - 30 * 60)}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at=head_pushed_at,
	)

	assert len(stalls) == 1, (
		f"Re-anchor must NOT apply to ai:implementing; got {stalls!r}"
	)
	assert stalls[0]["phase"] == "ai:implementing"


def test_detect_stalls_reanchor_fails_open_on_missing_entry():
	"""Fail-open: when the head-push mapping is provided but contains no
	entry for a given issue, the legacy ``status_since_ts`` anchor is used.
	"""
	now_ts = 1_700_000_000
	state, labels = _ai_done_state_past_threshold(now_ts)

	# Mapping is non-empty but lacks an entry for issue 10.
	head_pushed_at = {"99": _iso_z(now_ts - 30 * 60)}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at=head_pushed_at,
	)

	assert len(stalls) == 1, stalls
	assert stalls[0]["github_issue"] == 10


def test_detect_stalls_reanchor_fails_open_on_unparseable_timestamp():
	"""Fail-open: an unparseable ISO 8601 string must not crash detection
	and must not suppress the stall — the legacy anchor remains in effect.
	"""
	now_ts = 1_700_000_000
	state, labels = _ai_done_state_past_threshold(now_ts)

	head_pushed_at = {"10": "not-an-iso-8601-string"}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at=head_pushed_at,
	)

	assert len(stalls) == 1, stalls
	assert stalls[0]["github_issue"] == 10


def test_detect_stalls_reanchor_clamps_future_dated_head_push_to_now():
	"""Clock-skew defence: a future-dated ``headPushedAt`` (clock skew or a
	bogus value) must be clamped at ``now_ts`` rather than making the issue
	look perpetually fresh.  The clamped anchor still moves the effective
	elapsed to 0, so the issue is treated as fresh until wall-clock time
	reaches the future timestamp.  Tests the clamp specifically by
	asserting the issue drops out of the stalled set on this cycle.
	"""
	now_ts = 1_700_000_000
	state, labels = _ai_done_state_past_threshold(now_ts)

	# Timestamp three hours in the future — clamped at now_ts.
	head_pushed_at = {"10": _iso_z(now_ts + 3 * 60 * 60)}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at=head_pushed_at,
	)

	assert stalls == [], (
		f"Future-dated head push must clamp at now_ts, not propagate; got {stalls!r}"
	)


def test_detect_stalls_reanchor_parses_iso_with_microseconds_and_z_suffix():
	"""GitHub's GraphQL emits both ``2023-11-14T21:43:20Z`` and
	``2023-11-14T21:43:20.123456Z`` shapes.  Both must parse cleanly.
	"""
	now_ts = 1_700_000_000
	state, labels = _ai_done_state_past_threshold(now_ts)

	import datetime as _dt
	fresh_with_us = _dt.datetime.fromtimestamp(
		now_ts - 30 * 60, tz=_dt.timezone.utc
	).strftime("%Y-%m-%dT%H:%M:%S.123456Z")
	head_pushed_at = {"10": fresh_with_us}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at=head_pushed_at,
	)

	assert stalls == [], (
		f"ISO 8601 with microseconds + Z suffix must parse; got {stalls!r}"
	)


def test_detect_stalls_reanchor_no_op_when_head_pushed_at_is_none():
	"""Calling ``detect_stalls`` without the new kwarg must preserve every
	pre-existing semantic exactly — the new logic is opt-in via the kwarg.
	"""
	now_ts = 1_700_000_000
	state, labels = _ai_done_state_past_threshold(now_ts)

	stalls_legacy = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
	)
	stalls_none = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at=None,
	)
	stalls_empty = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=now_ts,
		head_pushed_at={},
	)

	assert stalls_legacy == stalls_none == stalls_empty
	assert len(stalls_legacy) == 1
	assert stalls_legacy[0]["github_issue"] == 10


def test_parse_iso8601_to_epoch_handles_common_shapes_and_failures():
	"""Direct coverage of the parser helper used by the ai:done re-anchor.

	The helper feeds untrusted strings from GitHub's GraphQL responses, so
	the contract is "return a finite integer epoch or None — never raise".
	"""
	# Z suffix (the common GitHub shape)
	assert orchestrate_lib._parse_iso8601_to_epoch("2023-11-14T21:43:20Z") == 1_699_998_200
	# Z suffix with microseconds
	assert orchestrate_lib._parse_iso8601_to_epoch("2023-11-14T21:43:20.500Z") == 1_699_998_200
	# Explicit +00:00 offset
	assert orchestrate_lib._parse_iso8601_to_epoch("2023-11-14T21:43:20+00:00") == 1_699_998_200
	# Non-UTC offset: 21:43:20+02:00 = 19:43:20Z = epoch 1_699_991_000
	# 1_699_998_200 - 2*3600 = 1_699_991_000
	assert orchestrate_lib._parse_iso8601_to_epoch("2023-11-14T21:43:20+02:00") == 1_699_991_000
	# Naive datetime (interpreted as UTC per GitHub contract)
	assert orchestrate_lib._parse_iso8601_to_epoch("2023-11-14T21:43:20") == 1_699_998_200

	# Failure modes — must return None, must not raise.
	assert orchestrate_lib._parse_iso8601_to_epoch("") is None
	assert orchestrate_lib._parse_iso8601_to_epoch(None) is None
	assert orchestrate_lib._parse_iso8601_to_epoch("not-a-date") is None
	assert orchestrate_lib._parse_iso8601_to_epoch(12345) is None
	assert orchestrate_lib._parse_iso8601_to_epoch([]) is None
	assert orchestrate_lib._parse_iso8601_to_epoch("2023-99-99T99:99:99Z") is None

def test_parse_iso8601_to_epoch_fails_open_on_timestamp_conversion_errors():
	"""``datetime.timestamp()`` can raise platform-specific conversion errors.

	The helper's fail-open contract must treat those the same as parse errors.
	"""
	original_datetime = orchestrate_lib.datetime

	def _fake_datetime_module(exc_type):
		class _FakeDatetimeModule:
			@staticmethod
			def fromisoformat(_s):
				class _FakeDT:
					tzinfo = object()

					def replace(self, **_kwargs):
						return self

					def timestamp(self):
						raise exc_type("boom")

				return _FakeDT()

		return _FakeDatetimeModule

	try:
		for exc_type in (OverflowError, OSError):
			orchestrate_lib.datetime = _fake_datetime_module(exc_type)
			assert orchestrate_lib._parse_iso8601_to_epoch("2023-11-14T21:43:20Z") is None
	finally:
		orchestrate_lib.datetime = original_datetime

def test_cmd_check_stalls_parses_head_pushed_at_json_and_threads_it_through(tmp_path=None):
	"""``cmd_check_stalls`` must accept ``--head-pushed-at-json`` and pass
	the parsed mapping into ``detect_stalls``.  Validates the CLI wiring
	added alongside the re-anchor.
	"""
	# tmp_path arg is optional so this test runs under both pytest and the
	# project's plain-python runner (which doesn't inject fixtures).
	import pathlib
	import shutil
	import tempfile

	td_owner = None
	if tmp_path is None:
		td_owner = tempfile.mkdtemp(prefix="stall_reanchor_cli_")
		tmp_path = pathlib.Path(td_owner)
	try:
		now_ts = 1_700_000_000
		state, _labels = _ai_done_state_past_threshold(now_ts)
		state_path = tmp_path / "state.json"
		state_path.write_text(json.dumps(state), encoding="utf-8")

		captured: dict = {}
		original = orchestrate_lib.detect_stalls

		def _capturing_detect_stalls(*args, **kwargs):
			captured["head_pushed_at"] = kwargs.get("head_pushed_at")
			return original(*args, **kwargs)

		orchestrate_lib.detect_stalls = _capturing_detect_stalls
		try:
			parser = orchestrate_lib.build_parser()
			args = parser.parse_args([
				"check-stalls",
				"--state-file", str(state_path),
				"--labels-json", json.dumps({"10": ["ai:done"], "11": ["ai:merged"]}),
				"--threshold-minutes", "120",
				"--now-ts", str(now_ts),
				"--head-pushed-at-json", json.dumps({"10": _iso_z(now_ts - 30 * 60), "11": ""}),
			])
			rc = orchestrate_lib.cmd_check_stalls(args)
		finally:
			orchestrate_lib.detect_stalls = original

		assert rc == 0
		assert captured.get("head_pushed_at") is not None
		# Empty-string entry for "11" must be filtered out by the CLI parser
		# so detect_stalls never sees it.
		assert "11" not in captured["head_pushed_at"]
		assert "10" in captured["head_pushed_at"]
	finally:
		if td_owner is not None:
			shutil.rmtree(td_owner, ignore_errors=True)


def test_resolve_stall_recovery_action_allows_human_terminalization_when_enabled():
	action = orchestrate_lib.resolve_stall_recovery_action(
		"ai:implementing",
		2,
		max_recoveries=5,
		enable_stall_human_terminalization=True,
	)

	assert action == "escalate_human"


def test_resolve_stall_recovery_action_fails_open_for_terminal_only_malformed_ladder():
	action = orchestrate_lib.resolve_stall_recovery_action(
		"ai:planning",
		2,
		max_recoveries=5,
		enable_stall_human_terminalization=False,
		actions_by_phase={"ai:planning": ["escalate_human"]},
	)

	assert action == "retrigger_pipeline"


def test_resolve_stall_recovery_action_phase_specific_cap_overrides_global_max():
	# At/above the global max_recoveries cap, the default behaviour returns
	# "skip" — see test_detect_stalls_still_skips_at_or_above_max_recoveries
	# below.  A per-phase override raises that cap so the configured ladder
	# runs to its natural end (e.g. retrigger_review → escalate_human for
	# ai:done) instead of devolving into a destructive skip.
	action_default = orchestrate_lib.resolve_stall_recovery_action(
		"ai:done",
		recovery_count=5,
		max_recoveries=5,
	)
	assert action_default == "skip"

	action_overridden = orchestrate_lib.resolve_stall_recovery_action(
		"ai:done",
		recovery_count=5,
		max_recoveries=5,
		max_recoveries_by_phase={"ai:done": 99},
	)
	assert action_overridden == "retrigger_review"

	# Override on an unrelated phase must not affect ai:done.
	action_unrelated = orchestrate_lib.resolve_stall_recovery_action(
		"ai:done",
		recovery_count=5,
		max_recoveries=5,
		max_recoveries_by_phase={"ai:clarification": 99},
	)
	assert action_unrelated == "skip"


def test_resolve_stall_recovery_action_phase_specific_cap_invalid_values_ignored():
	# Non-int / non-positive overrides must be ignored so a misconfigured
	# operator setting (e.g. MAX_STALL_RECOVERIES_DONE=0 or "bogus") cannot
	# accidentally raise the effective cap to 0 and turn every ai:done
	# recovery into "skip" on the first attempt.
	for bogus in (0, -1, "bogus", None, 3.5):
		action = orchestrate_lib.resolve_stall_recovery_action(
			"ai:done",
			recovery_count=5,
			max_recoveries=5,
			max_recoveries_by_phase={"ai:done": bogus},  # type: ignore[dict-item]
		)
		# Falls back to the global cap, which at recovery_count=5 returns skip.
		assert action == "skip", f"bogus override {bogus!r} should fall back to global cap"


def test_resolve_effective_stall_recovery_action_threads_phase_specific_cap():
	# `resolve_effective_stall_recovery_action` is the entry point used to
	# normalise stall-judge candidate actions; it must thread the per-phase
	# cap through to the fallback resolution so a candidate of None /
	# unrecognised string still respects the ai:done override.
	effective = orchestrate_lib.resolve_effective_stall_recovery_action(
		"ai:done",
		recovery_count=10,
		candidate_action=None,
		max_recoveries=5,
		max_recoveries_by_phase={"ai:done": 99},
	)
	assert effective == "retrigger_review"


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
		enable_stall_human_terminalization: bool = False,
		max_recoveries_by_phase: dict[str, int] | None = None,
		head_pushed_at: dict[str, str] | None = None,
	) -> list[dict[str, object]]:
		captured["state"] = state
		captured["issue_labels"] = issue_labels
		captured["threshold_minutes"] = threshold_minutes
		captured["now_ts"] = now_ts
		captured["max_recoveries"] = max_recoveries
		captured["phase_thresholds"] = phase_thresholds
		captured["max_recoveries_by_phase"] = max_recoveries_by_phase
		captured["stall_judge_trigger_count"] = stall_judge_trigger_count
		captured["enable_stall_judge"] = enable_stall_judge
		captured["enable_stall_human_terminalization"] = enable_stall_human_terminalization
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
		)
	finally:
		orchestrate_lib.detect_stalls = original_detect_stalls

	assert captured["threshold_minutes"] == 45
	assert captured["now_ts"] == 777
	assert captured["max_recoveries"] == 6
	assert captured["phase_thresholds"] == {"ai:planning": 90}
	assert captured["max_recoveries_by_phase"] is None
	assert captured["stall_judge_trigger_count"] == 3
	assert captured["enable_stall_judge"] is True
	assert captured["enable_stall_human_terminalization"] is False


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


def test_detect_stalls_human_terminalization_enabled_preserves_escalate_human():
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
		stall_judge_trigger_count=0,
		enable_stall_judge=False,
		enable_stall_human_terminalization=True,
	)
	assert len(result) == 1
	assert result[0]["recovery_action"] == "escalate_human"


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
		enable_stall_human_terminalization: bool = False,
		max_recoveries_by_phase: dict[str, int] | None = None,
		head_pushed_at: dict[str, str] | None = None,
	) -> list[dict[str, object]]:
		captured["state"] = state
		captured["issue_labels"] = issue_labels
		captured["threshold_minutes"] = threshold_minutes
		captured["now_ts"] = now_ts
		captured["max_recoveries"] = max_recoveries
		captured["phase_thresholds"] = phase_thresholds
		captured["max_recoveries_by_phase"] = max_recoveries_by_phase
		captured["stall_judge_trigger_count"] = stall_judge_trigger_count
		captured["enable_stall_judge"] = enable_stall_judge
		captured["enable_stall_human_terminalization"] = enable_stall_human_terminalization
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
		)
	finally:
		orchestrate_lib.detect_stalls = original_detect_stalls

	assert captured["threshold_minutes"] == 45
	assert captured["now_ts"] == 777
	assert captured["max_recoveries"] == 6
	assert captured["phase_thresholds"] == {"ai:planning": 90}
	assert captured["max_recoveries_by_phase"] is None
	assert captured["stall_judge_trigger_count"] == 3
	assert captured["enable_stall_judge"] is True
	assert captured["enable_stall_human_terminalization"] is False


def test_cmd_check_stalls_forwards_human_terminalization_flag_to_detect_stalls():
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
		enable_stall_human_terminalization: bool = False,
		max_recoveries_by_phase: dict[str, int] | None = None,
		head_pushed_at: dict[str, str] | None = None,
	) -> list[dict[str, object]]:
		captured["max_recoveries_by_phase"] = max_recoveries_by_phase
		captured["enable_stall_human_terminalization"] = enable_stall_human_terminalization
		return []

	orchestrate_lib.detect_stalls = _fake_detect_stalls
	try:
		state = _make_state()
		labels = {"10": ["ai:planning"], "11": ["ai:planning"]}
		_ = _run_check_stalls(
			state,
			labels,
			stall_judge_trigger_count=1,
			enable_stall_human_terminalization="true",
		)
	finally:
		orchestrate_lib.detect_stalls = original_detect_stalls

	assert captured["enable_stall_human_terminalization"] is True
	assert captured["max_recoveries_by_phase"] is None


def test_cmd_check_stalls_forwards_phase_specific_max_recoveries_to_detect_stalls():
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
		enable_stall_human_terminalization: bool = False,
		max_recoveries_by_phase: dict[str, int] | None = None,
		head_pushed_at: dict[str, str] | None = None,
	) -> list[dict[str, object]]:
		captured["max_recoveries_by_phase"] = max_recoveries_by_phase
		return []

	orchestrate_lib.detect_stalls = _fake_detect_stalls
	try:
		state = _make_state()
		labels = {"10": ["ai:done"], "11": ["ai:merged"]}
		_ = _run_check_stalls(
			state,
			labels,
			max_recoveries_by_phase_json='{"ai:done": 99}',
		)
	finally:
		orchestrate_lib.detect_stalls = original_detect_stalls

	assert captured["max_recoveries_by_phase"] == {"ai:done": 99}


def test_cmd_check_stalls_forwards_phase_specific_recovery_caps_to_detect_stalls():
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
		enable_stall_human_terminalization: bool = False,
		max_recoveries_by_phase: dict[str, int] | None = None,
		head_pushed_at: dict[str, str] | None = None,
	) -> list[dict[str, object]]:
		captured["max_recoveries_by_phase"] = max_recoveries_by_phase
		return []

	orchestrate_lib.detect_stalls = _fake_detect_stalls
	try:
		state = _make_state()
		labels = {"10": ["ai:done"], "11": ["ai:planning"]}
		_ = _run_check_stalls(
			state,
			labels,
			max_recoveries_by_phase_json='{"ai:done": 99}',
			stall_judge_trigger_count=1,
		)
	finally:
		orchestrate_lib.detect_stalls = original_detect_stalls

	assert captured["max_recoveries_by_phase"] == {"ai:done": 99}


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
	assert re.fullmatch(r"[0-9a-f]{64}", state["tracking_body_sync_hash"])
	assert re.fullmatch(r"[0-9a-f]{64}", state["tracking_body_last_readiness_refresh_hash"])
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
	fixture = json.loads(_fixture_path_by_name("child_footer_backticks").read_text(encoding="utf-8"))
	body = fixture["issues"]["1047"]["body"] + "\n### Wave 1\n\n- [ ] **issue-1**: First task (priority 1)\n"
	parsed = orchestrate_lib.parse_tracking_body(body)
	assert parsed["integration_branch"] == fixture["expected_stdout"]


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
	assert re.fullmatch(r"[0-9a-f]{64}", state["tracking_body_sync_hash"])
	assert re.fullmatch(r"[0-9a-f]{64}", state["tracking_body_last_readiness_refresh_hash"])
	assert state["final_merge_strategy"] == "squash"
	assert state["final_merge_pr"] is None
	assert state["final_merge_status"] == "pending"


def test_parse_tracking_body_captures_completion_marks():
	body = (
		"## Project: P\n\n"
		"### Wave 1\n\n"
		"- [x] **done-issue**: Finished task (priority 1)\n\n"
		"- [X] **done-issue-2**: Finished task (priority 2)\n\n"
		"### Wave 2\n\n"
		"- [ ] **todo-issue**: Pending task (priority 3)\n"
	)
	parsed = orchestrate_lib.parse_tracking_body(body)
	assert parsed["waves"][0][0]["completed"] is True
	assert parsed["waves"][0][1]["completed"] is True
	assert parsed["waves"][1][0]["completed"] is False


def test_rebuild_tracking_state_refuses_when_completed_issue_unmapped():
	"""Mirrors project #3627: wave 1 is already complete ([x]) but the child-
	issue search returned an empty map, so a from-scratch rebuild would reset
	current_wave to 1 and re-create the finished issue as a duplicate.  The
	rebuild must refuse rather than rewind."""
	body = (
		"## Project: P\n\n"
		"**Integration branch:** `orchestrator/project-9`\n\n"
		"### Wave 1\n\n- [x] **phase-h**: Phase H (priority 1)\n\n"
		"### Wave 2\n\n- [ ] **phase-d**: Phase D (priority 2)\n"
	)
	try:
		orchestrate_lib.rebuild_tracking_state(body, {}, tracking_issue=9)
	except orchestrate_lib.ReconstructionUnsafeError as exc:
		assert "phase-h" in str(exc)
		# It must be an OrchestrateError so the CLI maps it to a non-zero exit
		# and the poller falls into its "reconstruction failed, skipping" arm.
		assert isinstance(exc, orchestrate_lib.OrchestrateError)
	else:
		raise AssertionError("expected ReconstructionUnsafeError")


def test_rebuild_tracking_state_allows_when_completed_issue_mapped():
	"""When the completed issue IS discoverable, reconstruction proceeds and
	never queues it for re-creation."""
	body = (
		"## Project: P\n\n"
		"**Integration branch:** `orchestrator/project-9`\n\n"
		"### Wave 1\n\n- [x] **phase-h**: Phase H (priority 1)\n\n"
		"### Wave 2\n\n- [ ] **phase-d**: Phase D (priority 2)\n"
	)
	state = orchestrate_lib.rebuild_tracking_state(body, {"phase-h": 100}, tracking_issue=9)
	# The mapped, completed issue carries its real number and is not pending.
	assert state["issue_number_map"]["phase-h"] == 100
	assert "phase-h" not in state["pending_issue_defs"]
	w1_issue = state["waves"][0]["issues"][0]
	assert w1_issue["id"] == "phase-h"
	assert w1_issue["github_issue"] == 100
	# The still-pending, unmapped issue is legitimately queued for creation.
	assert "phase-d" in state["pending_issue_defs"]


def test_render_tracking_issue_body_from_state_preserves_structure_and_ticks_terminal_rows():
	template = """## Project: Test Project

Summary text stays the same.

---

**Total issues:** 4 | **Waves:** 2
**Integration branch:** `orchestrator/project-77`

### Wave 1

- [ ] **issue-1**: First task (priority 1)
- [ ] **issue-2**: Second task (priority 2)

### Wave 2

- [ ] **issue-3**: Third task (priority 3)
- [ ] **issue-4**: Fourth task (priority 4)

### Dependencies

- `issue-1` -> `issue-3`

---
*This issue is managed by the AI orchestrator. Do not edit manually.*
`ai:orchestrator-tracking`
"""
	state = _make_state(
		waves=[
			{
				"wave": 1,
				"issues": [
					{"id": "issue-1", "github_issue": 10, "status": "merged"},
					{"id": "issue-2", "github_issue": 11, "status": "closed"},
				],
			},
			{
				"wave": 2,
				"issues": [
					{"id": "issue-3", "github_issue": 12, "status": "skipped"},
					{"id": "issue-4", "github_issue": None, "status": "not_created"},
				],
			},
		],
	)
	state["project_body_snapshot"] = template

	rendered = orchestrate_lib.render_tracking_issue_body_from_state(state)

	assert "Summary text stays the same." in rendered
	assert "**Integration branch:** `orchestrator/project-77`" in rendered
	assert "### Dependencies" in rendered
	assert "- `issue-1` -> `issue-3`" in rendered
	assert rendered.rstrip().endswith("`ai:orchestrator-tracking`")
	assert "- [x] **issue-1**: First task (priority 1)" in rendered
	assert "- [x] **issue-2**: Second task (priority 2)" in rendered
	assert "- [x] **issue-3**: Third task (priority 3)" in rendered
	assert "- [ ] **issue-4**: Fourth task (priority 4)" in rendered


def test_render_tracking_issue_body_from_state_inserts_missing_issue_rows_into_existing_wave():
	template = """## Project: Test Project

### Wave 1

- [ ] **issue-1**: First task (priority 1)
"""
	state = _make_state()
	state["project_body_snapshot"] = template

	rendered = orchestrate_lib.render_tracking_issue_body_from_state(state)

	assert "- [ ] **issue-1**: First task (priority 1)" in rendered
	assert "- [ ] **issue-2**: #11" in rendered


def test_render_tracking_issue_body_from_state_rejects_missing_wave_heading_for_state_wave():
	template = """## Project: Test Project

### Wave 1

- [ ] **issue-1**: First task (priority 1)
"""
	state = _make_state(
		waves=[
			{
				"wave": 1,
				"issues": [{"id": "issue-1", "github_issue": 10, "status": "merged"}],
			},
			{
				"wave": 2,
				"issues": [{"id": "issue-2", "github_issue": 11, "status": "merged"}],
			},
		],
	)
	state["project_body_snapshot"] = template

	try:
		orchestrate_lib.render_tracking_issue_body_from_state(state)
		assert False, "Should have raised OrchestrateError"
	except orchestrate_lib.OrchestrateError as exc:
		assert "missing wave heading" in str(exc)


def test_resolve_integration_ref_parity_for_fixtures():
	fixtures = _iter_integration_fixtures()
	assert fixtures, "integration-ref fixtures are required"

	with tempfile.TemporaryDirectory() as tmpdir:
		bin_dir = Path(tmpdir)
		_write_mock_gh(bin_dir)
		for fixture in fixtures:
			fixture_path = _fixture_path_by_name(fixture["name"])
			expected_stdout = fixture["expected_stdout"]
			expected_exit = int(fixture["expected_exit_code"])

			bash_rc, bash_stdout, bash_stderr = _run_bash_resolver(fixture_path, bin_dir)
			python_rc, python_stdout, python_stderr = _run_python_resolver(fixture_path, bin_dir)

			assert bash_rc == expected_exit, f"bash exit mismatch for {fixture['name']}"
			assert python_rc == expected_exit, f"python exit mismatch for {fixture['name']}"
			assert bash_stdout == expected_stdout, f"bash stdout mismatch for {fixture['name']}"
			assert python_stdout == expected_stdout, f"python stdout mismatch for {fixture['name']}"
			assert bash_rc == python_rc, f"parity exit mismatch for {fixture['name']}"
			assert bash_stdout == python_stdout, f"parity stdout mismatch for {fixture['name']}"

			if expected_exit != 0:
				assert "::error::" in bash_stderr, f"bash missing ::error:: for {fixture['name']}"
				assert "::error::" in python_stderr, f"python missing ::error:: for {fixture['name']}"


def test_resolve_integration_ref_child_missing_tracking_present_fallback():
	fixture_path = _fixture_path_by_name("fallback_tracking_issue")
	fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
	expected_stdout = fixture["expected_stdout"]
	expected_exit = int(fixture["expected_exit_code"])

	with tempfile.TemporaryDirectory() as tmpdir:
		bin_dir = Path(tmpdir)
		_write_mock_gh(bin_dir)
		bash_rc, bash_stdout, _bash_stderr = _run_bash_resolver(fixture_path, bin_dir)
		python_rc, python_stdout, _python_stderr = _run_python_resolver(fixture_path, bin_dir)

	assert expected_stdout, "fallback fixture must resolve a tracking integration branch"
	assert expected_exit == 0, "fallback fixture should resolve successfully"
	assert bash_rc == expected_exit
	assert python_rc == expected_exit
	assert bash_stdout == expected_stdout
	assert python_stdout == expected_stdout


def test_resolve_integration_ref_shell_self_test():
	proc = subprocess.run(
		["bash", str(REPO_ROOT / "scripts" / "resolve_integration_ref.sh"), "--self-test"],
		check=False,
		capture_output=True,
		text=True,
		cwd=str(REPO_ROOT),
	)
	assert proc.returncode == 0, proc.stderr
	assert "self-test passed" in proc.stdout


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
# Partition guard: files_touched validation, hot-file loader, and
# auto-serialize sibling-overlap resolution
# ---------------------------------------------------------------------------


def test_validate_decomposition_accepts_missing_files_touched():
	"""Backward compat: issues without files_touched still validate, and the
	field is normalized to an empty list on the issue object."""
	data = _make_decomposition()
	validated = orchestrate_lib.validate_decomposition(data)
	for issue in validated["issues"]:
		assert issue["files_touched"] == [], f"expected empty list for {issue['id']}, got {issue['files_touched']!r}"


def test_validate_decomposition_normalizes_files_touched_paths():
	"""Paths are normalized: leading ./ stripped, backslashes → forward, dedup."""
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": [
			"./src/a.py",
			"src/a.py",
			"scripts\\b.sh",
			"  docs/c.md  ",
		]},
	])
	validated = orchestrate_lib.validate_decomposition(data)
	ft = validated["issues"][0]["files_touched"]
	assert "src/a.py" in ft
	assert ft.count("src/a.py") == 1, "duplicates should be collapsed"
	assert "scripts/b.sh" in ft
	assert "docs/c.md" in ft


def test_validate_decomposition_rejects_non_list_files_touched():
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": "README.md"},
	])
	try:
		orchestrate_lib.validate_decomposition(data)
	except orchestrate_lib.OrchestrateError as exc:
		assert "files_touched" in str(exc)
	else:
		raise AssertionError("expected OrchestrateError for non-list files_touched")


def test_validate_decomposition_rejects_empty_path_in_files_touched():
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["src/a.py", ""]},
	])
	try:
		orchestrate_lib.validate_decomposition(data)
	except orchestrate_lib.OrchestrateError as exc:
		assert "must not be empty" in str(exc)
	else:
		raise AssertionError("expected OrchestrateError for empty path")


def test_validate_decomposition_caps_files_touched_length():
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1,
		 "files_touched": [f"f{i}.py" for i in range(51)]},
	])
	try:
		orchestrate_lib.validate_decomposition(data)
	except orchestrate_lib.OrchestrateError as exc:
		assert "max 50" in str(exc)
	else:
		raise AssertionError("expected OrchestrateError for oversize files_touched")


def test_load_hot_files_missing_returns_empty_set(tmp_path_hack=None):
	with tempfile.TemporaryDirectory() as td:
		result = orchestrate_lib.load_hot_files(Path(td) / "does_not_exist.json")
		assert result == set()


def test_load_hot_files_parses_valid_registry():
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "hot_files.json"
		_write_json(p, {"hot_files": ["README.md", "./agents.md", "scripts\\x.sh", ""]})
		result = orchestrate_lib.load_hot_files(p)
		assert "README.md" in result
		assert "agents.md" in result, "./ prefix must be stripped"
		assert "scripts/x.sh" in result, "backslash must be normalized"
		assert "" not in result


def test_load_hot_files_malformed_returns_empty_set():
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "hot_files.json"
		p.write_text("not json", encoding="utf-8")
		assert orchestrate_lib.load_hot_files(p) == set()


def test_load_hot_files_wrong_shape_returns_empty_set():
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "hot_files.json"
		_write_json(p, {"something_else": ["x"]})
		assert orchestrate_lib.load_hot_files(p) == set()


def test_validate_wave_file_partition_empty_issues_no_overlap():
	"""Issues with empty files_touched are never flagged — the byte-level
	poller probe handles unknown-scope issues at merge time."""
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": []},
		{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": []},
	])
	data = orchestrate_lib.validate_decomposition(data)
	issues_by_id = {i["id"]: i for i in data["issues"]}
	overlaps = orchestrate_lib.validate_wave_file_partition(["a", "b"], issues_by_id)
	assert overlaps == []


def test_validate_wave_file_partition_detects_plain_pair_overlap():
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["src/x.py"]},
		{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["src/x.py", "src/y.py"]},
	])
	data = orchestrate_lib.validate_decomposition(data)
	issues_by_id = {i["id"]: i for i in data["issues"]}
	overlaps = orchestrate_lib.validate_wave_file_partition(["a", "b"], issues_by_id, hot_files=set())
	assert len(overlaps) == 1
	assert overlaps[0]["type"] == "pair"
	assert overlaps[0]["files"] == ["src/x.py"]
	assert {overlaps[0]["issue_a"], overlaps[0]["issue_b"]} == {"a", "b"}


def test_validate_wave_file_partition_separates_hot_file_category():
	"""An overlap entirely on hot files is reported as type=hot_file; a
	mixed overlap is reported twice (once hot_file, once pair)."""
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["README.md", "src/x.py"]},
		{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["README.md", "src/x.py"]},
		{"id": "c", "title": "T", "body": "b", "priority": 3, "files_touched": ["README.md"]},
		{"id": "d", "title": "T", "body": "b", "priority": 4, "files_touched": ["README.md"]},
	])
	data = orchestrate_lib.validate_decomposition(data)
	issues_by_id = {i["id"]: i for i in data["issues"]}
	overlaps = orchestrate_lib.validate_wave_file_partition(["a", "b", "c", "d"], issues_by_id, hot_files={"README.md"})

	hot_pairs = [o for o in overlaps if o["type"] == "hot_file"]
	plain_pairs = [o for o in overlaps if o["type"] == "pair"]
	# a vs b: both hot_file (README.md) and pair (src/x.py)
	# a vs c, a vs d, b vs c, b vs d, c vs d: hot_file only
	assert len(hot_pairs) == 6
	assert len(plain_pairs) == 1
	assert plain_pairs[0]["files"] == ["src/x.py"]


def test_auto_serialize_file_overlaps_basic_pair():
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["src/shared.py"]},
		{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["src/shared.py"]},
	])
	data = orchestrate_lib.validate_decomposition(data)
	serializations = orchestrate_lib.auto_serialize_file_overlaps(data, hot_files=set())
	assert len(serializations) == 1
	assert serializations[0]["winner"] == "a"  # lower priority number wins first-wave
	assert serializations[0]["loser"] == "b"
	# An edge a -> b should now exist
	assert {"from": "a", "to": "b"} in data["dependency_edges"]


def test_auto_serialize_file_overlaps_priority_tie_breaker_is_stable():
	data = _make_decomposition(issues=[
		{"id": "beta", "title": "T", "body": "b", "priority": 1, "files_touched": ["x.md"]},
		{"id": "alpha", "title": "T", "body": "b", "priority": 1, "files_touched": ["x.md"]},
	])
	data = orchestrate_lib.validate_decomposition(data)
	serializations = orchestrate_lib.auto_serialize_file_overlaps(data, hot_files=set())
	# Tie-break by lexicographic ID: alpha wins
	assert serializations[0]["winner"] == "alpha"
	assert serializations[0]["loser"] == "beta"


def test_auto_serialize_file_overlaps_cycle_guard_wires_to_detect_cycles():
	"""The cycle guard is defensive. In practice it is structurally
	unreachable (a pre-existing edge always separates the two siblings
	into different waves so no overlap is detected). This test verifies
	that the guard is wired up by monkey-patching _detect_cycles to
	raise, and asserting that auto_serialize_file_overlaps re-raises
	with a 'cycle' marker so the orchestrate.yml check-partition step
	surfaces the right diagnostic."""
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["x.py"]},
		{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["x.py"]},
	])
	data = orchestrate_lib.validate_decomposition(data)
	orig = orchestrate_lib._detect_cycles
	try:
		def _fake(ids, edges):
			# Raise only when the candidate edge (a -> b) is present, to
			# mimic a cycle being introduced by the serializer.
			if any(e.get("from") == "a" and e.get("to") == "b" for e in edges):
				raise orchestrate_lib.OrchestrateError("synthetic cycle for test")
		orchestrate_lib._detect_cycles = _fake
		try:
			orchestrate_lib.auto_serialize_file_overlaps(data, hot_files=set())
		except orchestrate_lib.OrchestrateError as exc:
			assert "cycle" in str(exc).lower()
		else:
			raise AssertionError("expected OrchestrateError for cycle")
	finally:
		orchestrate_lib._detect_cycles = orig


def test_compute_waves_auto_serializes_sibling_overlap():
	"""End-to-end: compute_waves() default path pushes the overlapping
	sibling into a later wave."""
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["README.md"]},
		{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["README.md"]},
		{"id": "c", "title": "T", "body": "b", "priority": 3, "files_touched": ["src/other.py"]},
	])
	data = orchestrate_lib.validate_decomposition(data)
	waves = orchestrate_lib.compute_waves(data, hot_files=set())
	# With auto-serialize, b is pushed to wave 2; a and c remain in wave 1
	assert len(waves) == 2
	w1_ids = {i["id"] for i in waves[0]}
	w2_ids = {i["id"] for i in waves[1]}
	assert w1_ids == {"a", "c"}
	assert w2_ids == {"b"}
	assert data["partition_serializations"], "serializations audit trail must be recorded"


def test_compute_waves_opt_out_of_auto_serialize_returns_raw_waves():
	"""auto_serialize=False yields the pre-rewrite wave layout."""
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["README.md"]},
		{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["README.md"]},
	])
	data = orchestrate_lib.validate_decomposition(data)
	waves = orchestrate_lib.compute_waves(data, hot_files=set(), auto_serialize=False)
	assert len(waves) == 1
	assert {i["id"] for i in waves[0]} == {"a", "b"}


def test_compute_waves_persists_files_touched_on_state_entries():
	"""build_tracking_state carries files_touched onto each wave entry so
	the poller can consult it without re-parsing issue bodies."""
	data = _make_decomposition(issues=[
		{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["src/a.py"]},
		{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["src/b.py"]},
	])
	data = orchestrate_lib.validate_decomposition(data)
	waves = orchestrate_lib.compute_waves(data, hot_files=set())
	state = orchestrate_lib.build_tracking_state(
		data=data,
		waves=waves,
		issue_number_map={"a": 100, "b": 101},
	)
	wave_issues = state["waves"][0]["issues"]
	ft_by_id = {i["id"]: i["files_touched"] for i in wave_issues}
	assert ft_by_id["a"] == ["src/a.py"]
	assert ft_by_id["b"] == ["src/b.py"]


def test_cli_check_partition_reports_planned_serializations():
	"""End-to-end CLI: check-partition emits planned rewrites without
	mutating the input file."""
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "decomp.json"
		_write_json(p, _make_decomposition(issues=[
			{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["README.md"]},
			{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["README.md"]},
		]))
		# Isolate from the real hot_files.json in CWD
		hot_p = Path(td) / "hot.json"
		_write_json(hot_p, {"hot_files": []})
		argv = [
			"check-partition",
			"--input-file", str(p),
			"--hot-files-path", str(hot_p),
		]
		# Capture stdout
		import io
		from contextlib import redirect_stdout
		buf = io.StringIO()
		with redirect_stdout(buf):
			rc = orchestrate_lib.main(argv)
		assert rc == 0, f"check-partition exit {rc}"
		report = json.loads(buf.getvalue())
		assert report["ok"] is True
		assert report["total_overlaps"] == 1
		assert len(report["planned_serializations"]) == 1
		# Input file should be untouched (check-partition is dry-run)
		roundtrip = json.loads(p.read_text(encoding="utf-8"))
		assert roundtrip["dependency_edges"] == []


def test_compute_effective_hot_files_no_telemetry_returns_seed():
	"""Zero-config baseline: no committed seed, no telemetry => empty set."""
	import orchestrate_lib as ol
	eff, audit = ol.compute_effective_hot_files(set())
	assert eff == set()
	assert audit["committed_seed_count"] == 0
	assert audit["learned_count"] == 0


def test_compute_effective_hot_files_only_committed_seed():
	import orchestrate_lib as ol
	eff, audit = ol.compute_effective_hot_files({"README.md", "src/a.py"})
	assert eff == {"README.md", "src/a.py"}
	assert audit["committed_seed_count"] == 2
	assert audit["learned_count"] == 0


def test_compute_effective_hot_files_learns_from_telemetry_meeting_threshold():
	import time
	import orchestrate_lib as ol
	now = int(time.time())
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "c.jsonl"
		records = [
			{"ts": now - 86400 * 1, "project": "A", "pr_a": 1, "pr_b": 2, "paths": ["scripts/god.sh"]},
			{"ts": now - 86400 * 5, "project": "A", "pr_a": 3, "pr_b": 4, "paths": ["scripts/god.sh"]},
			{"ts": now - 86400 * 10, "project": "B", "pr_a": 5, "pr_b": 6, "paths": ["scripts/god.sh", "README.md"]},
		]
		with p.open("w", encoding="utf-8") as f:
			for r in records:
				f.write(json.dumps(r) + "\n")
		eff, audit = ol.compute_effective_hot_files(set(), p)
		assert "scripts/god.sh" in eff, "3 events / 2 projects should promote"
		assert "README.md" not in eff, "1 event is below threshold"
		assert audit["learned_count"] == 1
		assert audit["learned_files"][0]["path"] == "scripts/god.sh"
		assert audit["learned_files"][0]["events"] == 3
		assert audit["learned_files"][0]["distinct_projects"] == 2


def test_compute_effective_hot_files_window_excludes_old_events():
	import time
	import orchestrate_lib as ol
	now = int(time.time())
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "c.jsonl"
		records = [
			# Plenty of events — but all outside the 90-day window.
			{"ts": now - 86400 * 120, "project": "A", "pr_a": 1, "pr_b": 2, "paths": ["old.py"]},
			{"ts": now - 86400 * 130, "project": "B", "pr_a": 3, "pr_b": 4, "paths": ["old.py"]},
			{"ts": now - 86400 * 140, "project": "C", "pr_a": 5, "pr_b": 6, "paths": ["old.py"]},
		]
		with p.open("w", encoding="utf-8") as f:
			for r in records:
				f.write(json.dumps(r) + "\n")
		eff, audit = ol.compute_effective_hot_files(set(), p, window_days=90)
		assert "old.py" not in eff, "events outside window should not promote"
		assert audit["telemetry_events_total"] == 3
		assert audit["telemetry_events_in_window"] == 0
		assert audit["learned_count"] == 0


def test_compute_effective_hot_files_distinct_projects_required():
	"""Even with many events, a single project cannot promote on its own."""
	import time
	import orchestrate_lib as ol
	now = int(time.time())
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "c.jsonl"
		records = [
			{"ts": now - 86400 * i, "project": "only_one", "pr_a": i, "pr_b": i + 100, "paths": ["runaway.py"]}
			for i in range(1, 10)
		]
		with p.open("w", encoding="utf-8") as f:
			for r in records:
				f.write(json.dumps(r) + "\n")
		eff, audit = ol.compute_effective_hot_files(set(), p, min_distinct_projects=2)
		assert "runaway.py" not in eff, "single project should not promote even with high event count"
		assert audit["telemetry_events_in_window"] == 9
		assert audit["learned_count"] == 0


def test_compute_effective_hot_files_unions_seed_and_learned():
	"""Committed seed and learned telemetry compose via set union."""
	import time
	import orchestrate_lib as ol
	now = int(time.time())
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "c.jsonl"
		records = [
			{"ts": now - 86400 * 1, "project": "A", "pr_a": 1, "pr_b": 2, "paths": ["learned.py"]},
			{"ts": now - 86400 * 2, "project": "A", "pr_a": 3, "pr_b": 4, "paths": ["learned.py"]},
			{"ts": now - 86400 * 3, "project": "B", "pr_a": 5, "pr_b": 6, "paths": ["learned.py"]},
		]
		with p.open("w", encoding="utf-8") as f:
			for r in records:
				f.write(json.dumps(r) + "\n")
		eff, audit = ol.compute_effective_hot_files({"seeded.py"}, p)
		assert eff == {"seeded.py", "learned.py"}
		assert audit["committed_seed_count"] == 1
		assert audit["learned_count"] == 1


def test_compute_effective_hot_files_handles_missing_file_gracefully():
	"""A missing JSONL path is equivalent to an empty telemetry source."""
	import orchestrate_lib as ol
	eff, audit = ol.compute_effective_hot_files({"seed.py"}, "/nonexistent/path/merge.jsonl")
	assert eff == {"seed.py"}
	assert audit["learned_count"] == 0


def test_compute_effective_hot_files_skips_malformed_records():
	"""Non-JSON lines and records with wrong shapes are silently skipped."""
	import time
	import orchestrate_lib as ol
	now = int(time.time())
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "c.jsonl"
		lines = [
			"not json at all",
			"{}",  # empty object
			'{"ts": "not_an_int", "project": "X", "paths": ["x.py"]}',
			'{"ts": %d, "project": "A", "paths": "not-a-list"}' % now,
			# Three valid records across two projects meeting threshold
			'{"ts": %d, "project": "A", "paths": ["valid.py"]}' % (now - 86400),
			'{"ts": %d, "project": "A", "paths": ["valid.py"]}' % (now - 86400 * 2),
			'{"ts": %d, "project": "B", "paths": ["valid.py"]}' % (now - 86400 * 3),
		]
		p.write_text("\n".join(lines) + "\n", encoding="utf-8")
		eff, audit = ol.compute_effective_hot_files(set(), p)
		assert "valid.py" in eff
		assert audit["telemetry_events_total"] >= 3


def test_compute_effective_hot_files_normalizes_learned_paths():
	"""Learned paths are normalized the same way the committed seed is."""
	import time
	import orchestrate_lib as ol
	now = int(time.time())
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "c.jsonl"
		records = [
			{"ts": now - 86400 * 1, "project": "A", "pr_a": 1, "pr_b": 2, "paths": ["./src/a.py"]},
			{"ts": now - 86400 * 2, "project": "A", "pr_a": 3, "pr_b": 4, "paths": ["src\\a.py"]},
			{"ts": now - 86400 * 3, "project": "B", "pr_a": 5, "pr_b": 6, "paths": ["src/a.py"]},
		]
		with p.open("w", encoding="utf-8") as f:
			for r in records:
				f.write(json.dumps(r) + "\n")
		eff, audit = ol.compute_effective_hot_files(set(), p)
		assert eff == {"src/a.py"}, f"paths should normalize to a single entry, got {eff}"
		assert audit["learned_files"][0]["events"] == 3


def test_cli_check_partition_uses_telemetry_jsonl():
	"""End-to-end CLI: check-partition accepts --conflict-telemetry-jsonl
	and promotes learned hot files into the effective set."""
	import time
	import orchestrate_lib as ol
	now = int(time.time())
	with tempfile.TemporaryDirectory() as td:
		decomp_p = Path(td) / "decomp.json"
		_write_json(decomp_p, _make_decomposition(issues=[
			{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["scripts/god.sh"]},
			{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["scripts/god.sh"]},
		]))
		tel_p = Path(td) / "tel.jsonl"
		hot_p = Path(td) / "hot.json"
		_write_json(hot_p, {"hot_files": []})
		records = [
			{"ts": now - 86400 * 1, "project": "A", "pr_a": 1, "pr_b": 2, "paths": ["scripts/god.sh"]},
			{"ts": now - 86400 * 2, "project": "A", "pr_a": 3, "pr_b": 4, "paths": ["scripts/god.sh"]},
			{"ts": now - 86400 * 3, "project": "B", "pr_a": 5, "pr_b": 6, "paths": ["scripts/god.sh"]},
		]
		with tel_p.open("w", encoding="utf-8") as f:
			for r in records:
				f.write(json.dumps(r) + "\n")

		argv = [
			"check-partition",
			"--input-file", str(decomp_p),
			"--hot-files-path", str(hot_p),
			"--conflict-telemetry-jsonl", str(tel_p),
		]
		import io
		from contextlib import redirect_stdout
		buf = io.StringIO()
		with redirect_stdout(buf):
			rc = ol.main(argv)
		assert rc == 0
		report = json.loads(buf.getvalue())
		assert "scripts/god.sh" in report["hot_files"]
		assert report["hot_files_audit"]["learned_count"] == 1
		# The overlap is reported as hot_file (not just plain pair) because
		# the learned set now includes scripts/god.sh.
		hot_overlaps = [o for o in report["wave_reports"][0]["overlaps"] if o["type"] == "hot_file"]
		assert len(hot_overlaps) >= 1


def test_cli_compute_waves_uses_telemetry_for_serialization():
	"""Auto-serialize uses the effective (seed ∪ learned) hot-file set."""
	import time
	import orchestrate_lib as ol
	now = int(time.time())
	with tempfile.TemporaryDirectory() as td:
		decomp_p = Path(td) / "decomp.json"
		_write_json(decomp_p, _make_decomposition(issues=[
			{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["shared.md"]},
			{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["shared.md"]},
		]))
		hot_p = Path(td) / "hot.json"
		_write_json(hot_p, {"hot_files": []})
		tel_p = Path(td) / "tel.jsonl"
		records = [
			{"ts": now - 86400 * 1, "project": "A", "pr_a": 1, "pr_b": 2, "paths": ["shared.md"]},
			{"ts": now - 86400 * 2, "project": "A", "pr_a": 3, "pr_b": 4, "paths": ["shared.md"]},
			{"ts": now - 86400 * 3, "project": "B", "pr_a": 5, "pr_b": 6, "paths": ["shared.md"]},
		]
		with tel_p.open("w", encoding="utf-8") as f:
			for r in records:
				f.write(json.dumps(r) + "\n")
		argv = [
			"compute-waves",
			"--input-file", str(decomp_p),
			"--hot-files-path", str(hot_p),
			"--conflict-telemetry-jsonl", str(tel_p),
			"--write-back",
		]
		import io
		from contextlib import redirect_stdout
		buf = io.StringIO()
		with redirect_stdout(buf):
			rc = ol.main(argv)
		assert rc == 0
		report = json.loads(buf.getvalue())
		assert "shared.md" in report["hot_files"]
		# b should be serialized into wave 2 because shared.md is learned hot
		assert report["total_waves"] == 2


def test_cli_compute_waves_write_back_mutates_input():
	"""compute-waves --write-back persists serialized edges back to disk."""
	with tempfile.TemporaryDirectory() as td:
		p = Path(td) / "decomp.json"
		_write_json(p, _make_decomposition(issues=[
			{"id": "a", "title": "T", "body": "b", "priority": 1, "files_touched": ["x.md"]},
			{"id": "b", "title": "T", "body": "b", "priority": 2, "files_touched": ["x.md"]},
		]))
		hot_p = Path(td) / "hot.json"
		_write_json(hot_p, {"hot_files": []})
		argv = [
			"compute-waves",
			"--input-file", str(p),
			"--hot-files-path", str(hot_p),
			"--write-back",
		]
		import io
		from contextlib import redirect_stdout
		buf = io.StringIO()
		with redirect_stdout(buf):
			rc = orchestrate_lib.main(argv)
		assert rc == 0
		roundtrip = json.loads(p.read_text(encoding="utf-8"))
		assert {"from": "a", "to": "b"} in roundtrip["dependency_edges"]
		assert roundtrip.get("partition_serializations")


# ---------------------------------------------------------------------------
# Tests: phase_attempts lifetime counter (fix 1a)
# ---------------------------------------------------------------------------

def test_phase_attempts_counter_survives_phase_oscillation():
	"""Regression test for the loop where an autofix run nudges an issue
	out of ai:review-blocked into ai:done and back, zeroing
	stall_recovery_count each time and re-running the ladder forever.

	After this fix, increment_stall_recovery bumps a phase-scoped
	lifetime counter that update_issue_timestamps does NOT reset on
	phase change, so the cap is reached even when phase flaps.
	"""
	state = _make_state()
	issue = state["waves"][0]["issues"][0]

	# Simulate three recovery cycles in ai:review-blocked with a phase
	# flap to ai:done between each one.  After every flap,
	# update_issue_timestamps zeroes stall_recovery_count.
	now_ts = 1_000_000
	for cycle in range(3):
		# Stall observed: bump both counters.
		orchestrate_lib.increment_stall_recovery(state, issue["id"], phase="ai:review-blocked")
		# Phase flap: autofix flicks ai:review-blocked off and back on.
		orchestrate_lib.update_issue_timestamps(
			state,
			issue_labels={str(issue["github_issue"]): ["ai:done"]},
			now_ts=now_ts + (cycle * 100),
		)
		orchestrate_lib.update_issue_timestamps(
			state,
			issue_labels={str(issue["github_issue"]): ["ai:review-blocked"]},
			now_ts=now_ts + (cycle * 100) + 50,
		)

	# stall_recovery_count was zeroed by the last phase change.
	assert issue["stall_recovery_count"] == 0
	# phase_attempts survived all three oscillations.
	assert issue["phase_attempts"]["ai:review-blocked"] == 3


def test_phase_attempts_count_caps_recovery_action():
	"""When phase_attempts_count reaches max_recoveries, the resolver
	returns 'skip' even if stall_recovery_count is 0."""
	action = orchestrate_lib.resolve_stall_recovery_action(
		"ai:review-blocked",
		recovery_count=0,
		max_recoveries=5,
		phase_attempts_count=5,
	)
	assert action == "skip"

	# Below the cap, the ladder still runs.
	action = orchestrate_lib.resolve_stall_recovery_action(
		"ai:review-blocked",
		recovery_count=0,
		max_recoveries=5,
		phase_attempts_count=4,
	)
	assert action == "dispatch_rb_judge"


def test_detect_stalls_skips_when_phase_attempts_exhausted():
	"""detect_stalls returns recovery_action='skip' when phase_attempts
	reaches the cap, even with a fresh stall_recovery_count of 0."""
	state = _make_state()
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0  # zeroed by phase oscillation
	issue["phase_attempts"] = {"ai:review-blocked": 5}
	labels = {"10": ["ai:review-blocked"], "11": ["ai:merged"]}

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
	assert stalls[0]["phase_attempts_count"] == 5
def test_detect_stalls_honors_phase_specific_cap_for_phase_attempts():
	"""Phase-attempt caps must honour per-phase recovery overrides too."""
	state = _make_state()
	issue = state["waves"][0]["issues"][0]
	issue["status"] = "in_progress"
	issue["status_since_ts"] = 1
	issue["stall_recovery_count"] = 0
	issue["phase_attempts"] = {"ai:done": 5}
	labels = {"10": ["ai:done"], "11": ["ai:merged"]}

	stalls = orchestrate_lib.detect_stalls(
		state=state,
		issue_labels=labels,
		threshold_minutes=120,
		now_ts=8 * 60 * 60,
		max_recoveries=5,
		enable_stall_judge=False,
		max_recoveries_by_phase={"ai:done": 99},
	)

	assert len(stalls) == 1
	assert stalls[0]["recovery_action"] == "retrigger_review"
	assert stalls[0]["phase_attempts_count"] == 5


def test_increment_stall_recovery_without_phase_is_backward_compatible():
	"""Old callers that pass only (state, issue_id) still work and do
	not create a phase_attempts dict."""
	state = _make_state()
	orchestrate_lib.increment_stall_recovery(state, "issue-1")
	issue = state["waves"][0]["issues"][0]
	assert issue["stall_recovery_count"] == 1
	assert "phase_attempts" not in issue


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
