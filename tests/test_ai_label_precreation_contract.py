#!/usr/bin/env python3
"""Static contract: workflows must ensure ai:* labels before mutation."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

WORKFLOW_CONTRACT = {
	".github/workflows/orchestrate.yml": {
		"must_contain": [
			re.compile(r'(?m)^\s*(?:source|\.)\s+["\']?(?:\./)?scripts/label_helpers\.sh["\']?(?=[\s;#]|$)'),
			'ensure_label_exists "ai:orchestrator-tracking" "${{ github.repository }}"',
			'ensure_label_exists "ai:clarification" "${{ github.repository }}"',
			'ensure_label_exists "ai:orchestrator-managed" "${{ github.repository }}"',
			'ensure_label_exists "ai:orchestrator-validate-required" "${{ github.repository }}"',
		],
		"must_not_contain": [
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:orchestrator-tracking['\"]?"),
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:clarification['\"]?"),
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:orchestrator-managed['\"]?"),
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:orchestrator-validate-required['\"]?"),
		],
	},
	".github/workflows/comprehensive-test-and-release.yml": {
		"must_contain": [
			re.compile(r'(?m)^\s*(?:source|\.)\s+["\']?(?:\./)?scripts/label_helpers\.sh["\']?(?=[\s;#]|$)'),
			'ensure_label_exists "${PENDING_LABEL}" "${GITHUB_REPOSITORY}"',
		],
		"must_not_contain": [
			re.compile(r'gh\s+api\s+[\'\"]?repos/\$\{GITHUB_REPOSITORY\}/labels[\'\"]?\s+--method\s+POST'),
		],
	},
	".github/workflows/validation-improvements-intake.yml": {
		"must_contain": [
			re.compile(r'for\s+f\s+in\s+[^;\n]*label_helpers\.sh[^;\n]*;\s*do\b'),
			re.compile(r'(?m)^\s*(?:source|\.)\s+["\']?(?:\./)?scripts/label_helpers\.sh["\']?(?=[\s;#]|$)'),
			'ensure_label_exists "ai:needs-prompt-review" "${GITHUB_REPOSITORY}"',
		],
		"must_not_contain": [
			re.compile(r'gh\s+label\s+list\s+--limit\s+500'),
			re.compile(r"gh\s+label\s+create\s+['\"]?ai:needs-prompt-review['\"]?"),
		],
	},
	".github/workflows/issue_pr_status.yml": {
		"must_contain": [
			re.compile(r'for\s+f\s+in\s+[^;\n]*label_helpers\.sh[^;\n]*;\s*do\b'),
			re.compile(r'(?m)^\s*(?:source|\.)\s+["\']?(?:\./)?scripts/label_helpers\.sh["\']?(?=[\s;#]|$)'),
			'ensure_label_exists "${FINAL_LABEL}" "${REPOSITORY}"',
			'set_issue_phase_label_resilient "${issue_number}" "${FINAL_LABEL}" "${REPOSITORY}"',
			'PR_TITLE: ${{ github.event.pull_request.title }}',
			'PR_BODY: ${{ github.event.pull_request.body }}',
			'_pr_title="${PR_TITLE:-}"',
			'_pr_body="${PR_BODY:-}"',
			'PR_DATA="${_pr_title} ${_pr_body}"',
			'if [ -z "${PR_DATA//[[:space:]]/}" ]; then',
		],
		"must_not_contain": [
			re.compile(r'repos/\$\{REPOSITORY\}/labels/\$\(printf\s+[\'\"]?%s[\'\"]?\s+[\'\"]?\$\{FINAL_LABEL\}[\'\"]?\)'),
			re.compile(r'_AI_PHASE_LABELS\s*='),
		],
	},
	".github/workflows/review_autofix.yml": {
		"must_contain": [
			'set_issue_phase_label_resilient "${issue_number}" "ai:ready-to-merge" "${REPOSITORY}"',
			'set_issue_phase_label_resilient "${issue_number}" "ai:review-blocked" "${REPOSITORY}"',
			re.compile(r'_delete_failed=0'),
			re.compile(r'if\s+\[\s+"\$\{_delete_failed\}"\s+-eq\s+1\s+\];\s*then\s*\n\s*echo\s+"::warning::set_issue_phase_label_resilient: some phase-label deletions failed[^\n]*\n\s*return\s+0\s*\n\s*fi'),
		],
		"must_not_contain": [
			re.compile(r'_AI_PHASE_LABELS\s*='),
		],
	},
}


def _workflow_text(rel_path: str) -> str:
	path = REPO_ROOT / rel_path
	return path.read_text(encoding="utf-8")


def _contains(text: str, pattern: object) -> bool:
	if isinstance(pattern, re.Pattern):
		return bool(pattern.search(text))
	return str(pattern) in text


def test_ai_label_precreation_contract() -> None:
	for rel_path, contract in WORKFLOW_CONTRACT.items():
		text = _workflow_text(rel_path)

		for expected in contract["must_contain"]:
			assert _contains(text, expected), f"{rel_path} missing required pattern: {expected}"

		for forbidden in contract["must_not_contain"]:
			assert not _contains(text, forbidden), f"{rel_path} contains forbidden legacy probe/mutation: {forbidden}"


def test_issue_pr_status_payload_fallback_order() -> None:
	text = _workflow_text(".github/workflows/issue_pr_status.yml")
	payload_idx = text.find('PR_DATA="${_pr_title} ${_pr_body}"')
	refetch_idx = text.find('gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}" --jq')
	assert payload_idx != -1, "issue_pr_status.yml missing payload-first PR_DATA assembly"
	assert refetch_idx != -1, "issue_pr_status.yml missing fallback PR refetch call"
	assert payload_idx < refetch_idx, "issue_pr_status.yml should parse payload title/body before PR refetch fallback"


if __name__ == "__main__":
	test_ai_label_precreation_contract()
	test_issue_pr_status_payload_fallback_order()
	print("PASS")
