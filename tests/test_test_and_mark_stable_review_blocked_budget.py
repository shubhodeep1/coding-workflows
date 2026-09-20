#!/usr/bin/env python3
"""Contract checks for the release gate's review-blocked E2E budget and run pinning."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test-and-mark-stable.yml"


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


def test_success_transitions_and_unconditional_cleanup_are_preserved() -> None:
	workflow = _read_workflow()
	phase6 = _phase6(workflow)
	assert 'if [ "$REVIEW_BLOCKED_PRESENT" -eq 0 ] || [ "$TERMINAL_LABEL_PRESENT" -eq 1 ]; then' in phase6
	assert "ai:merged|ai:ready-to-merge" in phase6
	cleanup = _slice_between(workflow, "- name: Cleanup test artifacts", "# Temporary: reveal which git invocation")
	assert "if: always()" in cleanup
	assert 'issues/${TRACKING_NUMBER}' in cleanup


def main() -> int:
	tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
