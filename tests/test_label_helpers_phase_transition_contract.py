#!/usr/bin/env python3
"""Contract tests for resilient phase-label transitions in label helpers."""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
LABEL_HELPERS = REPO_ROOT / "scripts" / "label_helpers.sh"


def _script_text() -> str:
	return LABEL_HELPERS.read_text(encoding="utf-8")


def _function_body(text: str, function_name: str) -> str:
	match = re.search(rf"(?ms)^{function_name}\(\) \{{\n(.*?)^\}}", text)
	assert match is not None, f"Missing function: {function_name}"
	return match.group(1)


def test_set_issue_phase_label_resilient_contract() -> None:
	text = _script_text()
	body = _function_body(text, "set_issue_phase_label_resilient")

	assert "_AI_PHASE_LABELS='[\"ai:done\"" in text
	assert 'ensure_label_exists "${target_label}" "${repo}" || true' in body
	assert '--argjson p "${_AI_PHASE_LABELS}" --arg t "${target_label}"' in body
	assert "'(. - $p) + [$t] | unique'" in body
	assert 'gh_retry gh api --paginate "repos/${repo}/issues/${issue_number}/labels"' in body
	assert 'gh_retry gh api -X PUT "repos/${repo}/issues/${issue_number}/labels"' in body
	assert 'gh_retry gh api -X POST "repos/${repo}/issues/${issue_number}/labels"' in body
	assert body.count("return 0") >= 4


def test_ensure_label_exists_backward_compatible_contract() -> None:
	text = _script_text()
	body = _function_body(text, "ensure_label_exists")

	assert "gh_retry gh label create" in body
	assert "already[ _-]*exists|already_exists" in body
	assert "return 1" in body


if __name__ == "__main__":
	test_set_issue_phase_label_resilient_contract()
	test_ensure_label_exists_backward_compatible_contract()
	print("PASS")
