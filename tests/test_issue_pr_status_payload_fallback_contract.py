#!/usr/bin/env python3
"""Contract tests for payload-first fallback in issue_pr_status workflow."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def test_payload_first_fallback_and_shared_helper_usage() -> None:
	text = _workflow_text()

	assert "PR_TITLE: ${{ github.event.pull_request.title }}" in text
	assert "PR_BODY: ${{ github.event.pull_request.body || '' }}" in text
	assert 'PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"' in text
	assert 'set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}"' in text
	assert "_AI_PHASE_LABELS='[\"ai:done\"" not in text

	payload_pos = text.find('PR_DATA="${PR_TITLE:-} ${PR_BODY:-}"')
	fetch_pos = text.find('PR_DATA="$(gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq')
	assert payload_pos != -1
	assert fetch_pos != -1
	assert payload_pos < fetch_pos, "Fallback GH pull fetch must only run after payload text check"


if __name__ == "__main__":
	test_payload_first_fallback_and_shared_helper_usage()
	print("PASS")
