#!/usr/bin/env python3
"""Contract checks for the release gate's review-blocked E2E budget and run pinning."""

from __future__ import annotations

import re
from pathlib import Path
from runpy import run_path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"
MARK_STABLE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mark-stable.yml"
extract_refs = run_path(str(REPO_ROOT / "scripts" / "check_workflow_script_refs.py"))["extract_refs"]


def _read_workflow() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _slice_between(text: str, start_marker: str, end_marker: str) -> str:
	start = text.find(start_marker)
	assert start != -1, f"Missing marker: {start_marker}"
	end = text.find(end_marker, start)
	assert end != -1, f"Missing marker after {start_marker}: {end_marker}"
	return text[start:end]


def _e2e_job(workflow: str) -> str:
	return _slice_between(workflow, "  e2e-smoke-test:\n", "  validate-scripts:\n")


def _phase6(workflow: str) -> str:
	return _slice_between(
		workflow,
		'# ── Phase 6: Review-blocked simulation test',
		'# ── Deep verification: inspect every step in every workflow run',
	)


def _integer_contract_value(job: str, name: str) -> int:
	match = re.search(rf"^\s+{re.escape(name)}:\s+(\d+)\s*$", job, re.MULTILINE)
	assert match is not None, f"Missing integer contract value: {name}"
	return int(match.group(1))


def _workflow_dispatch_integer_default(workflow: str, input_name: str) -> int:
	match = re.search(
		rf"^      {re.escape(input_name)}:\s*$.*?^        default:\s+(\d+)\s*$",
		workflow,
		re.MULTILINE | re.DOTALL,
	)
	assert match is not None, f"Missing workflow_dispatch integer default: {input_name}"
	return int(match.group(1))


def test_reference_extraction_ignores_only_full_line_comments() -> None:
	workflow_text = """
      # scripts/yaml_comment_only.sh
      run: |
        # ${SUPPORT_SCRIPTS_DIR}/shell_comment_only.py
        # COMMENTED_SCRIPTS="commented_assignment.sh"
        # for f in ${COMMENTED_SCRIPTS}; do
        #   cp "scripts/${f}" /tmp/
        # done
        scripts/active_reference.sh # scripts/inline_comment_reference.sh
        printf '%s\\n' '# scripts/quoted_hash_reference.sh'
"""
	refs = extract_refs(workflow_text)
	assert "yaml_comment_only.sh" not in refs
	assert "shell_comment_only.py" not in refs
	assert "commented_assignment.sh" not in refs
	assert "active_reference.sh" in refs
	assert "inline_comment_reference.sh" in refs
	assert "quoted_hash_reference.sh" in refs


def test_release_workflows_use_canonical_reference_checker() -> None:
	checker_call = "PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_workflow_script_refs.py"
	raw_scanner = "grep -rhoE 'scripts/[a-zA-Z0-9_.-]+\\.(sh|py|json)'"
	for workflow_path in (WORKFLOW, MARK_STABLE_WORKFLOW):
		workflow = workflow_path.read_text(encoding="utf-8")
		check_step = _slice_between(
			workflow,
			"      - name: Script-workflow cross-reference\n",
			"\n      - name:",
		)
		assert checker_call in check_step
		assert raw_scanner not in check_step
		assert "OPTIONAL_SCRIPTS=" not in check_step


def test_default_serial_budget_leaves_required_headroom() -> None:
	workflow = _read_workflow()
	job = _e2e_job(workflow)
	timeout_match = re.search(r"^\s+timeout-minutes:\s+(\d+)\s*$", job, re.MULTILINE)
	assert timeout_match is not None
	job_timeout = int(timeout_match.group(1))
	assert job_timeout == _integer_contract_value(job, "E2E_JOB_TIMEOUT_MINUTES") == 300

	phase_timeout = _workflow_dispatch_integer_default(workflow, "phase_timeout")
	plan_timeout = _workflow_dispatch_integer_default(workflow, "plan_timeout")
	review_timeout = _workflow_dispatch_integer_default(workflow, "review_timeout")
	review_step_timeout = _workflow_dispatch_integer_default(workflow, "review_step_timeout")
	review_handoff_budget = _integer_contract_value(job, "REVIEW_PHASE_HANDOFF_BUDGET_MINUTES")
	finalization_reserve = _integer_contract_value(job, "E2E_FINALIZATION_RESERVE_MINUTES")
	review_effective_budget = max(review_timeout, min(review_step_timeout, 90) + review_handoff_budget)
	serial_budget = (
		(2 * phase_timeout)
		+ plan_timeout
		+ review_effective_budget
		+ _integer_contract_value(job, "EDITOR_RETRY_BUDGET_MINUTES")
		+ phase_timeout
		+ _integer_contract_value(job, "PHASE7_WAIT_BUDGET_MINUTES")
		+ finalization_reserve
	)
	assert review_handoff_budget == 15
	assert review_effective_budget == 90
	assert serial_budget == 295
	assert serial_budget <= job_timeout
	assert job_timeout - (serial_budget - finalization_reserve) >= finalization_reserve


def test_budget_guard_runs_before_artifact_creation() -> None:
	job = _e2e_job(_read_workflow())
	guard = 'if [ "${E2E_SERIAL_BUDGET_MINUTES}" -gt "${E2E_JOB_TIMEOUT_MINUTES}" ]; then'
	create_issue = "- name: Create E2E test issue"
	assert guard in job
	assert 'echo "::error::Configured E2E phase budgets require' in job
	assert "exit 1" in job[job.index(guard) : job.index(create_issue)]
	assert job.index(guard) < job.index(create_issue)
	assert "(2 * PHASE_TIMEOUT)" in job
	assert "REVIEW_EFFECTIVE_BUDGET_MINUTES" in job
	assert "REVIEW_EFFECTIVE_BUDGET_MINUTES=$((REVIEW_EFFECTIVE_BUDGET_MINUTES + REVIEW_PHASE_HANDOFF_BUDGET_MINUTES))" in job
	assert "REVIEW_PHASE_WALL_BUDGET_MINUTES=$((REVIEW_STEP_TIMEOUT + REVIEW_PHASE_HANDOFF_BUDGET_MINUTES))" in job
	assert 'REVIEW_PHASE_DEADLINE=$((REVIEW_PHASE_STARTED_AT + (REVIEW_PHASE_WALL_BUDGET_MINUTES * 60)))' in job
	assert 'if [ "$NOW" -ge "$REVIEW_PHASE_DEADLINE" ]; then' in job


def test_budget_guard_clamps_review_step_timeout_before_serial_math() -> None:
	workflow = _read_workflow()
	job = _e2e_job(workflow)
	prerequisites = _slice_between(job, "- name: Validate prerequisites", "# Fast-fail the hottest")
	step_budget = 'REVIEW_EFFECTIVE_BUDGET_MINUTES="${REVIEW_STEP_TIMEOUT}"'
	cap_guard = 'if [ "${REVIEW_EFFECTIVE_BUDGET_MINUTES}" -gt 90 ]; then'
	handoff_budget = "REVIEW_EFFECTIVE_BUDGET_MINUTES=$((REVIEW_EFFECTIVE_BUDGET_MINUTES + REVIEW_PHASE_HANDOFF_BUDGET_MINUTES))"
	review_max = 'if [ "${REVIEW_TIMEOUT}" -gt "${REVIEW_EFFECTIVE_BUDGET_MINUTES}" ]; then'
	assert prerequisites.index(step_budget) < prerequisites.index(cap_guard) < prerequisites.index(handoff_budget)
	assert prerequisites.index(handoff_budget) < prerequisites.index(review_max)
	assert 'REVIEW_EFFECTIVE_BUDGET_MINUTES=90' in prerequisites
	assert 'REVIEW_STEP_TIMEOUT_MAX=90' in job

	phase_timeout = _workflow_dispatch_integer_default(workflow, "phase_timeout")
	plan_timeout = _workflow_dispatch_integer_default(workflow, "plan_timeout")
	review_timeout = _workflow_dispatch_integer_default(workflow, "review_timeout")
	review_step_override = _workflow_dispatch_integer_default(workflow, "review_step_timeout") + 30
	review_handoff_budget = _integer_contract_value(job, "REVIEW_PHASE_HANDOFF_BUDGET_MINUTES")
	shared_budget = (
		(2 * phase_timeout)
		+ plan_timeout
		+ _integer_contract_value(job, "EDITOR_RETRY_BUDGET_MINUTES")
		+ phase_timeout
		+ _integer_contract_value(job, "PHASE7_WAIT_BUDGET_MINUTES")
		+ _integer_contract_value(job, "E2E_FINALIZATION_RESERVE_MINUTES")
	)
	job_timeout = _integer_contract_value(job, "E2E_JOB_TIMEOUT_MINUTES")
	assert shared_budget + max(review_timeout, review_step_override) > job_timeout
	assert shared_budget + max(review_timeout, min(review_step_override, 90) + review_handoff_budget) > job_timeout


def test_named_retry_and_phase7_budgets_replace_literal_deadlines() -> None:
	job = _e2e_job(_read_workflow())
	assert "DEADLINE=$(( $(date +%s) + (EDITOR_RETRY_BUDGET_MINUTES * 60) ))" in job
	assert job.count("(PHASE7_WAIT_BUDGET_MINUTES * 60)") == 2


def test_phase6_registers_once_and_polls_only_the_pinned_run() -> None:
	phase6 = _phase6(_read_workflow())
	branch_query = "runs?event=workflow_dispatch&branch=${POLLER_DISPATCH_REF}&per_page=10"
	dispatch = 'gh workflow run "${workflow_file}" --repo "${TEST_REPO}" --ref "${POLLER_DISPATCH_REF}"'
	assert branch_query in phase6
	assert dispatch in phase6
	assert phase6.index(branch_query) < phase6.index(dispatch)
	assert phase6.count('echo "poller_run_id=${POLLER_RUN_ID}" >> "$GITHUB_OUTPUT"') == 1
	assert '"repos/${TEST_REPO}/actions/runs/${POLLER_RUN_ID}"' in phase6
	assert 'actions/workflows/${workflow_file}/dispatches' not in phase6
	assert "workflow_run_id" not in phase6
	assert "actions/runs?per_page=5&event=workflow_dispatch" not in phase6
	assert "POLLER_LATEST_ID" not in phase6


def test_phase6_rejects_a_newer_foreign_ref_dispatch() -> None:
	phase6 = _phase6(_read_workflow())
	assert '--arg dispatch_ref "${POLLER_DISPATCH_REF}"' in phase6
	assert '.head_branch == $dispatch_ref' in phase6
	assert '.id > $baseline_id' in phase6
	assert 'fromdateiso8601? // 0) >= ($dispatch_started_epoch - 5)' in phase6
	assert 'if [ "${candidate_count}" -gt 1 ]; then' in phase6
	assert 'Ambiguous ${workflow_file} registration' in phase6


def test_phase6_transient_api_errors_remain_bounded() -> None:
	phase6 = _phase6(_read_workflow())
	assert phase6.count("retrying within the inactivity window") == 3
	assert 'echo "status=timeout" >> "$GITHUB_OUTPUT"' in phase6
	labels_read = 'if ! LABELS=$(gh_api_safe_quiet_print'
	assert labels_read in phase6
	labels_block = phase6[phase6.index(labels_read) : phase6.index("# If rate-limited")]
	assert '|| echo ""' not in labels_block
	assert 'sleep "$POLL_INTERVAL"' in labels_block
	assert "continue" in labels_block
	assert 'issues/${TRACKING_NUMBER}' in labels_block
	assert "exit 0" in labels_block
	assert "exit 1" not in labels_block
	transient_read = 'if ! POLLER_RUN=$(gh_api_safe_quiet_print'
	label_progress = 'if [ "$LABELS" != "$PREV_LABELS" ]; then'
	success_guard = 'if [ "$REVIEW_BLOCKED_PRESENT" -eq 0 ] || [ "$TERMINAL_LABEL_PRESENT" -eq 1 ]; then'
	assert phase6.index(label_progress) < phase6.index(transient_read)
	assert phase6.index(success_guard) < phase6.index(transient_read)
	read_complete = 'POLLER_STATUS=$(printf'
	read_block = phase6[phase6.index(transient_read) : phase6.index(read_complete)]
	assert 'echo "status=poller_failed"' not in read_block
	assert read_block.count('issues/${TRACKING_NUMBER}') == 2
	assert read_block.count("exit 0") == 2
	assert "exit 1" not in read_block


def test_reviewer_majority_is_progress_only() -> None:
	job = _e2e_job(_read_workflow())
	progress_block = _slice_between(
		job,
		'if [ "$SUCCEEDED" -ge 3 ] && [ $((SUCCEEDED * 2)) -gt "$TOTAL_DONE" ]; then',
		'elif [ "$TOTAL_DONE" -gt 0 ]; then',
	)
	assert "waiting for authoritative review completion and editor verification" in progress_block
	assert "status=success" not in progress_block
	assert "review_run_id=" not in progress_block
	assert "exit 0" not in progress_block
	assert 'if [ "$RUN_STATUS" = "completed" ]; then' in job
	assert 'if [ "$RUN_CONCLUSION" = "success" ]; then' in job
	assert 'echo "status=success" >> "$GITHUB_OUTPUT"' in job
	assert 'if [ "${PR_HEAD}" = "${BAIT_SHA}" ]; then' in job


def test_success_transitions_and_unconditional_cleanup_are_preserved() -> None:
	workflow = _read_workflow()
	phase6 = _phase6(workflow)
	assert 'if [ "$REVIEW_BLOCKED_PRESENT" -eq 0 ] || [ "$TERMINAL_LABEL_PRESENT" -eq 1 ]; then' in phase6
	assert "ai:merged|ai:ready-to-merge" in phase6
	cleanup = _slice_between(workflow, "- name: Cleanup test artifacts", "# Temporary: reveal which git invocation")
	assert "if: always()" in cleanup
	assert 'issues/${TRACKING_NUMBER}' in cleanup


def _phase4b(workflow: str) -> str:
	return _slice_between(
		workflow,
		'- name: "Phase 4b: Verify editor restored canary (pytest + retry)"',
		"# ── Phase 5: Orchestrator script integration test",
	)


def _adopt_jq(phase4b: str, prior: str, bait: str, head: str) -> str:
	match = re.search(r'ADOPTED_REVIEW_RUN_ID=\$\(gh_api_with_retry \\\n.*?--jq "(.*?)"\) \|\| ADOPTED_REVIEW_RUN_ID=""', phase4b, re.DOTALL)
	assert match is not None, "Phase 4b must look up an already-active review run before dispatching"
	return (
		match.group(1)
		.replace('\\"', '"')
		.replace("${PRIOR_REVIEW_RUN}", prior)
		.replace("${BAIT_SHA}", bait)
		.replace("${RETRY_DISPATCH_SHA}", head)
	)


def _run_jq(program: str, payload: dict) -> str:
	import json
	import subprocess

	proc = subprocess.run(["jq", "-r", program], input=json.dumps(payload), capture_output=True, text=True, check=True)
	return proc.stdout.strip()


BAIT = "c9662b3d92b90b076a447d048ace6c5121d638ba"
PRE_BAIT = "8527974652020072c0ffc99fcf9887ddb4c6ccf8"


def test_phase4b_adopts_oldest_active_review_run_instead_of_dispatching() -> None:
	"""Run 35802596362: at Phase 4b's retry (02:23) the Phase 3c fallback
	dispatch and the bait synchronize run were still active on the bait
	SHA. A fresh dispatch queued behind them until 03:04; the gate must
	wait on the oldest active one instead."""
	program = _adopt_jq(_phase4b(_read_workflow()), "35803994060", BAIT, BAIT)
	payload = {"workflow_runs": [
		{"id": 35804161376, "status": "in_progress", "head_sha": BAIT, "created_at": "2026-09-23T00:55:49Z"},
		{"id": 35804156977, "status": "in_progress", "head_sha": BAIT, "created_at": "2026-09-23T00:55:46Z"},
		{"id": 35803994060, "status": "completed", "head_sha": PRE_BAIT, "created_at": "2026-09-23T00:53:28Z"},
	]}
	assert _run_jq(program, payload) == "35804156977"


def test_phase4b_ignores_prior_completed_and_foreign_sha_runs() -> None:
	program = _adopt_jq(_phase4b(_read_workflow()), "35803994060", BAIT, BAIT)
	payload = {"workflow_runs": [
		{"id": 35803994060, "status": "in_progress", "head_sha": BAIT, "created_at": "2026-09-23T00:53:28Z"},
		{"id": 2, "status": "completed", "head_sha": BAIT, "created_at": "2026-09-23T00:55:46Z"},
		{"id": 3, "status": "queued", "head_sha": PRE_BAIT, "created_at": "2026-09-23T00:50:00Z"},
	]}
	assert _run_jq(program, payload) == ""


def test_phase4b_dispatches_only_when_no_run_is_adopted() -> None:
	phase4b = _phase4b(_read_workflow())
	lookup = phase4b.index("ADOPTED_REVIEW_RUN_ID=$(gh_api_with_retry")
	dispatch = phase4b.index('if ! gh workflow run "${REVIEW_WORKFLOW_FILE}"')
	prior_guard = phase4b.index('if ! [[ "${PRIOR_REVIEW_RUN}" =~ ^[0-9]+$ ]]')
	assert prior_guard < lookup < dispatch
	assert "runs?branch=${BRANCH}&per_page=100" in phase4b
	else_branch = phase4b[phase4b.index('if [[ "${ADOPTED_REVIEW_RUN_ID}" =~ ^[0-9]+$ ]]; then'):dispatch]
	assert "else" in else_branch and 'ADOPTED_REVIEW_RUN_ID=""' in else_branch
	assert '"repos/${TEST_REPO}/actions/runs/${ADOPTED_REVIEW_RUN_ID}"' in phase4b
	assert "did not complete within ${EDITOR_RETRY_BUDGET_MINUTES} minutes" in phase4b
	assert "${{" not in phase4b.split("run: |", 1)[1]
def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
