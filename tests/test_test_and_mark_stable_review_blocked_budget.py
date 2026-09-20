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


def test_default_serial_budget_leaves_required_headroom() -> None:
	job = _e2e_job(_read_workflow())
	timeout_match = re.search(r"^\s+timeout-minutes:\s+(\d+)\s*$", job, re.MULTILINE)
	assert timeout_match is not None
	job_timeout = int(timeout_match.group(1))
	assert job_timeout == _integer_contract_value(job, "E2E_JOB_TIMEOUT_MINUTES") == 300

	phase_timeout = 30
	plan_timeout = 60
	review_timeout = 60
	review_step_timeout = 75
	serial_budget = (
		(2 * phase_timeout)
		+ plan_timeout
		+ max(review_timeout, review_step_timeout)
		+ _integer_contract_value(job, "EDITOR_RETRY_BUDGET_MINUTES")
		+ phase_timeout
		+ _integer_contract_value(job, "PHASE7_WAIT_BUDGET_MINUTES")
		+ _integer_contract_value(job, "E2E_FINALIZATION_RESERVE_MINUTES")
	)
	assert serial_budget == 280
	assert job_timeout - serial_budget == 20


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


def test_named_retry_and_phase7_budgets_replace_literal_deadlines() -> None:
	job = _e2e_job(_read_workflow())
	assert "DEADLINE=$(( $(date +%s) + (EDITOR_RETRY_BUDGET_MINUTES * 60) ))" in job
	assert job.count("(PHASE7_WAIT_BUDGET_MINUTES * 60)") == 2


def test_phase6_registers_once_and_polls_only_the_pinned_run() -> None:
	phase6 = _phase6(_read_workflow())
	snapshot = 'actions/workflows/${workflow_file}/runs?event=workflow_dispatch&per_page=1'
	dispatch = 'gh workflow run "${workflow_file}" --repo "${TEST_REPO}"'
	assert snapshot in phase6
	assert dispatch in phase6
	assert phase6.index(snapshot) < phase6.index(dispatch)
	assert 'actions/workflows/${workflow_file}/runs?event=workflow_dispatch&per_page=10' in phase6
	assert 'if [ "${candidate_count}" -gt 1 ]; then' in phase6
	assert "refusing to guess a run id" in phase6
	assert phase6.count('echo "poller_run_id=${POLLER_RUN_ID}" >> "$GITHUB_OUTPUT"') == 1
	assert '"repos/${TEST_REPO}/actions/runs/${POLLER_RUN_ID}"' in phase6
	assert "actions/runs?per_page=5&event=workflow_dispatch" not in phase6
	assert "POLLER_LATEST_ID" not in phase6


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
