#!/usr/bin/env python3
"""Regression contract for issue_pr_status payload-first PR fallback parsing."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ISSUE_PR_STATUS_WF = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"


def _workflow_text() -> str:
	return ISSUE_PR_STATUS_WF.read_text(encoding="utf-8")


def test_issue_pr_status_prefers_event_payload_before_rest_fallback() -> None:
	wf = _workflow_text()
	payload_assign = 'PR_DATA="${PR_TITLE} ${PR_BODY}"'
	guard = 'if [ -z "$(printf \'%s\' "${PR_DATA}" | tr -d \'[:space:]\')" ]; then'

	assert payload_assign in wf
	assert guard in wf
	assert 'PR_TITLE: ${{ github.event.pull_request.title || \'\' }}' in wf
	assert 'PR_BODY: ${{ github.event.pull_request.body || \'\' }}' in wf

	payload_idx = wf.index(payload_assign)
	guard_idx = wf.index(guard)
	rest_idx = wf.index('gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq')
	assert payload_idx < guard_idx < rest_idx


def test_issue_pr_status_uses_shared_phase_label_transition_helper() -> None:
	wf = _workflow_text()
	assert 'set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}"' in wf
	assert 'gh_retry gh api -X PUT "repos/${REPOSITORY}/issues/${issue_number}/labels"' not in wf


def main() -> int:
	test_issue_pr_status_prefers_event_payload_before_rest_fallback()
	test_issue_pr_status_uses_shared_phase_label_transition_helper()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
