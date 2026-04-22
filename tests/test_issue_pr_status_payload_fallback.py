#!/usr/bin/env python3
"""Contract tests for payload-first fallback logic in issue_pr_status.yml."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "issue_pr_status.yml"


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _update_labels_block(text: str) -> str:
	match = re.search(
		r"- name: Update linked issue labels when PR closes[\s\S]*?- name: Finalize linked issue lineage state",
		text,
	)
	assert match is not None, "Could not locate update-labels run block in issue_pr_status.yml"
	return match.group(0)


def test_payload_env_is_wired() -> None:
	text = _workflow_text()
	block = _update_labels_block(text)
	assert "PR_TITLE: ${{ github.event.pull_request.title }}" in block
	assert "PR_BODY: ${{ github.event.pull_request.body }}" in block


def test_fallback_prefers_payload_before_refetch() -> None:
	text = _workflow_text()
	block = _update_labels_block(text)

	assert '_pr_title="${PR_TITLE:-}"' in block
	assert '_pr_body="${PR_BODY:-}"' in block
	assert 'PR_DATA="${_pr_title} ${_pr_body}"' in block
	assert 'if [ -z "${PR_DATA//[[:space:]]/}" ]; then' in block

	payload_idx = block.find('PR_DATA="${_pr_title} ${_pr_body}"')
	refetch_idx = block.find('gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq')
	assert payload_idx != -1 and refetch_idx != -1
	assert payload_idx < refetch_idx, "PR payload should be used before API refetch fallback"


def main() -> int:
	test_payload_env_is_wired()
	test_fallback_prefers_payload_before_refetch()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
