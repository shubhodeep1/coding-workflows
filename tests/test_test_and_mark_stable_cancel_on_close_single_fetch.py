#!/usr/bin/env python3
"""Regression checks for the closed-PR cancel-on-close wait loop."""

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


def _closed_pr_existing_run_branch(wf: str) -> str:
	return _slice_between(
		wf,
		'PR #${PR_NUMBER} is already closed (head_sha=${PR_HEAD_SHA}); looking up the cancel-on-close run that fired for that closure.',
		'# PR is still open — exercise the close path end to end.',
	)


def _existing_run_wait_loop(branch: str) -> str:
	return _slice_between(
		branch,
		'while [ "${EXISTING_STATUS}" != "completed" ] && [ "$(date +%s)" -lt "${WAIT_DEADLINE}" ]; do',
		'if [ "${EXISTING_STATUS}" != "completed" ]; then',
	)


def _open_pr_close_branch(wf: str) -> str:
	return _slice_between(
		wf,
		"# PR is still open — exercise the close path end to end.",
		"# Per-phase soft-error analysis: cancel-on-pr-close",
	)


def _new_run_wait_loop(branch: str) -> str:
	return _slice_between(
		branch,
		'while [ "$(date +%s)" -lt "${DEADLINE}" ]; do',
		'if [ -z "${NEW_RUN_ID}" ]; then',
	)


def test_closed_pr_wait_loop_fetches_existing_run_once_per_iteration() -> None:
	wf = _read_workflow()
	loop = _existing_run_wait_loop(_closed_pr_existing_run_branch(wf))
	fetch_fragment = 'gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}"'

	assert loop.count(fetch_fragment) == 1
	assert 'EXISTING_RUN_JSON=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" 2>/dev/null || echo "")' in loop
	assert "--jq '.status // \"\"'" not in loop
	assert "--jq '.conclusion // \"\"'" not in loop


def test_closed_pr_wait_loop_derives_status_and_conclusion_from_shared_payload() -> None:
	wf = _read_workflow()
	loop = _existing_run_wait_loop(_closed_pr_existing_run_branch(wf))
	fetch_stmt = 'EXISTING_RUN_JSON=$(gh api "repos/${TEST_REPO}/actions/runs/${EXISTING_RUN_ID}" 2>/dev/null || echo "")'
	status_stmt = "EXISTING_STATUS=$(printf '%s' \"${EXISTING_RUN_JSON}\" | jq -r '.status // \"\"' 2>/dev/null || echo \"\")"
	conclusion_stmt = "EXISTING_CONCLUSION=$(printf '%s' \"${EXISTING_RUN_JSON}\" | jq -r '.conclusion // \"\"' 2>/dev/null || echo \"\")"

	assert fetch_stmt in loop
	assert status_stmt in loop
	assert conclusion_stmt in loop
	assert loop.index(fetch_stmt) < loop.index(status_stmt) < loop.index(conclusion_stmt)


def test_phase7_records_pre_close_mergeability_diagnostics() -> None:
	wf = _read_workflow()
	assert "--jq '{state, head_sha: .head.sha, closed_at, mergeable, mergeable_state}'" in wf
	assert 'echo "Pre-close PR diagnostics: mergeable=${PR_MERGEABLE}, mergeable_state=${PR_MERGEABLE_STATE}"' in wf


def test_phase7_accepts_event_or_scheduled_cleanup_runs() -> None:
	wf = _read_workflow()
	closed_branch = _closed_pr_existing_run_branch(wf)
	open_branch = _open_pr_close_branch(wf)
	for branch in (closed_branch, open_branch):
		assert '.event == "pull_request"' in branch
		assert '.event == "schedule"' in branch
		assert '.created_at >= $closed_at' in branch


def test_open_pr_wait_loop_fetches_workflow_list_once_and_fails_on_api_error() -> None:
	wf = _read_workflow()
	loop = _new_run_wait_loop(_open_pr_close_branch(wf))
	# The listing goes through phase7_list_cancel_runs, which retries a
	# transient error up to 3 times (run 36389883126) and still fails the
	# loop with lookup_failed after that.
	fetch_fragment = "phase7_list_cancel_runs 10"
	assert loop.count(fetch_fragment) == 1
	assert "if ! phase7_list_cancel_runs 10; then" in loop
	assert "|| echo" not in loop
	assert 'echo "status=lookup_failed" >> "$GITHUB_OUTPUT"' in loop



def _no_run_branch(branch: str) -> str:
	return _slice_between(
		branch,
		'if [ -z "${NEW_RUN_ID}" ]; then',
		"exit 1",
	)


def test_open_pr_wait_loop_backoff_caps_at_30s_and_clamps_to_deadline() -> None:
	wf = _read_workflow()
	loop = _new_run_wait_loop(_open_pr_close_branch(wf))
	assert "PHASE7_POLL_SLEEP=3" in loop
	for value in ("1) PHASE7_POLL_SLEEP=10 ;;", "2) PHASE7_POLL_SLEEP=20 ;;", "*) PHASE7_POLL_SLEEP=30 ;;"):
		assert value in loop
	sleeps = re.findall(r"PHASE7_POLL_SLEEP=(\d+)", loop)
	assert sleeps and max(int(s) for s in sleeps) == 30
	assert re.search(r"\bsleep [0-9]", loop) is None
	remaining = 'PHASE7_POLL_REMAINING=$(( DEADLINE - $(date +%s) ))'
	brk = 'if [ "${PHASE7_POLL_REMAINING}" -le 0 ]; then'
	clamp = 'if [ "${PHASE7_POLL_SLEEP}" -ge "${PHASE7_POLL_REMAINING}" ]; then'
	# The clamped sleep ends one second before DEADLINE so one last
	# listing still runs inside the budget.
	assert 'PHASE7_POLL_SLEEP=$(( PHASE7_POLL_REMAINING - 1 ))' in loop
	sleep_stmt = 'sleep "${PHASE7_POLL_SLEEP}"'
	assert loop.index(remaining) < loop.index(brk) < loop.index(clamp) < loop.index(sleep_stmt)
	assert loop.count(sleep_stmt) == 1


def test_open_pr_no_run_diagnostic_precedes_error() -> None:
	wf = _read_workflow()
	open_branch = _open_pr_close_branch(wf)
	start = open_branch.index('if [ -z "${NEW_RUN_ID}" ]; then')
	tail = open_branch[start:]
	group = tail.index('echo "::group::Phase 7 no-run diagnostic"')
	endgroup = tail.index('echo "::endgroup::"')
	error = tail.index('echo "::error::internal-cancel-on-pr-close did not register a new run within timeout"')
	status = tail.index('echo "status=no_run" >> "$GITHUB_OUTPUT"')
	assert group < endgroup < error < status
	diag = tail[group:endgroup]
	for field in ("closed_at=", "pre_run_id=", "polls=", "elapsed_secs=", "pr=${PR_NUMBER}"):
		assert field in diag
	for field in ("id=", "event=", "status=", "conclusion=", "head_sha=", "created_at="):
		assert field in diag
	assert "[neutralised]" in diag
	assert '[[:cntrl:]]' in diag


def test_no_run_diagnostic_makes_no_second_listing() -> None:
	wf = _read_workflow()
	diag = _no_run_branch(_open_pr_close_branch(wf))
	assert "phase7_list_cancel_runs" not in diag
	assert "/runs?" not in diag
	assert "${CANDIDATE_RUNS_JSON:-}" in diag
	assert diag.count("gh api") == 1
	state_read = 'gh api "repos/${TEST_REPO}/actions/workflows/internal-cancel-on-pr-close.yml" --jq \'.state // ""\''
	assert state_read in diag
	assert diag.index("# \u00a715: the runs listing above carries no workflow state") < diag.index(state_read)
	assert 'PHASE7_CANCEL_WORKFLOW_STATE="unknown"' in diag
	assert "${{" not in diag


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
