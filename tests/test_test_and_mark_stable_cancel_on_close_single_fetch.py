#!/usr/bin/env python3
"""Regression checks for the closed-PR cancel-on-close wait loop."""

from __future__ import annotations

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
	fetch_fragment = '"repos/${TEST_REPO}/actions/workflows/internal-cancel-on-pr-close.yml/runs?per_page=10"'
	assert loop.count(fetch_fragment) == 1
	assert "if ! CANDIDATE_RUNS_JSON=$(gh api" in loop
	assert "|| echo" not in loop
	assert 'echo "status=lookup_failed" >> "$GITHUB_OUTPUT"' in loop


def main() -> int:
	tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
	for test in tests:
		test()
	print(f"{len(tests)} passed")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
