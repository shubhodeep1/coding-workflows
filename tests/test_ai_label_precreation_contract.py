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
		],
		"must_not_contain": [
			re.compile(r'repos/\$\{REPOSITORY\}/labels/\$\(printf\s+[\'\"]?%s[\'\"]?\s+[\'\"]?\$\{FINAL_LABEL\}[\'\"]?\)'),
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


if __name__ == "__main__":
	test_ai_label_precreation_contract()
	print("PASS")
