#!/usr/bin/env python3
"""Contract tests for review_autofix shared phase-label helper usage."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


META_FALLBACK = 'PR_DATA="$(jq -r \'[.title // "", .body // ""] | join(" ")\' "${PR_META_FILE}" 2>/dev/null || echo "")"'
JQ_FALLBACK_EXPR = "--jq '.title + \" \" + (.body // \"\")'"
GH_FETCH_CALL = 'gh_retry gh api "repos/${REPOSITORY}/pulls/${PR_NUMBER}"'
SAFE_FETCH_CALL = '_safe_gh_jq "repos/${REPOSITORY}/pulls/${PR_NUMBER}"'
SHARED_FALLBACK_ENV = 'LINKED_ISSUE_FALLBACK_NUMBERS_JSON'
SHARED_HELPER_PREFIX = 'extract_repo_scoped_issue_refs_from_text "${REPOSITORY}"'
BROAD_ISSUE_MENTION = 'issue[[:space:]]*#[[:space:]]*[0-9]+'
BROAD_ISSUES_PATH = '(^|[^[:alnum:]_/-])issues/[0-9]+'
COLON_FALLBACK = '(closes|fixes|resolves)[[:space:]]*:?[[:space:]]*#[[:space:]]*[0-9]+'


def _workflow_text() -> str:
	return WORKFLOW.read_text(encoding="utf-8")


def _step_block(text: str, step_name: str) -> str:
	marker = f"- name: {step_name}"
	start = text.find(marker)
	assert start != -1, f"Missing workflow step: {step_name}"
	next_step = text.find("\n      - name:", start + len(marker))
	if next_step == -1:
		return text[start:]
	return text[start:next_step]


def test_target_steps_use_shared_helper_and_remove_inline_phase_array() -> None:
	text = _workflow_text()

	assert "_AI_PHASE_LABELS='[\"ai:done\"" not in text
	assert 'set_issue_phase_label_resilient "${issue_number}" "ai:ready-to-merge" "${REPOSITORY}"' in text
	assert text.count('set_issue_phase_label_resilient "${issue_number}" "ai:review-blocked" "${REPOSITORY}"') >= 2

	for step_name in (
		"Mark linked issues ready to merge",
		"Mark linked issues review-blocked (autofix exhaustion)",
		"Mark linked issues review-blocked (workflow failure)",
	):
		block = _step_block(text, step_name)
		assert SHARED_FALLBACK_ENV in block, f"{step_name}: missing shared fallback env reuse"
		assert SHARED_HELPER_PREFIX in block, f"{step_name}: missing shared strict fallback helper"
		assert BROAD_ISSUE_MENTION not in block, f"{step_name}: bare issue mentions must stay non-actionable"
		assert BROAD_ISSUES_PATH not in block, f"{step_name}: bare issues/N paths must stay non-actionable"
		assert COLON_FALLBACK not in block, f"{step_name}: colon-form closing fallback must stay disabled"

	validate_block = _step_block(text, "Dispatch standalone validate for orchestrator short-circuit issues")
	assert SHARED_HELPER_PREFIX in validate_block, "post-merge validate dispatch must use the shared strict fallback helper"
	assert BROAD_ISSUE_MENTION not in validate_block
	assert BROAD_ISSUES_PATH not in validate_block
	assert COLON_FALLBACK not in validate_block


def test_pr_meta_fallback_precedes_pull_fetch_in_target_steps() -> None:
	text = _workflow_text()
	checks = [
		("Mark linked issues ready to merge", GH_FETCH_CALL),
		("Mark linked issues review-blocked (autofix exhaustion)", SAFE_FETCH_CALL),
		("Mark linked issues review-blocked (workflow failure)", GH_FETCH_CALL),
	]

	for step_name, fetch_call in checks:
		block = _step_block(text, step_name)
		meta_pos = block.find(META_FALLBACK)
		fetch_call_pos = block.find(fetch_call)
		fetch_jq_pos = block.find(JQ_FALLBACK_EXPR)
		assert meta_pos != -1, f"{step_name}: missing PR_META_FILE fallback"
		assert fetch_call_pos != -1, f"{step_name}: missing fallback GH PR fetch call"
		assert fetch_jq_pos != -1, f"{step_name}: missing fallback GH PR fetch jq expression"
		assert meta_pos < fetch_call_pos, f"{step_name}: expected PR_META_FILE fallback before GH fetch"


if __name__ == "__main__":
	test_target_steps_use_shared_helper_and_remove_inline_phase_array()
	test_pr_meta_fallback_precedes_pull_fetch_in_target_steps()
	print("PASS")
