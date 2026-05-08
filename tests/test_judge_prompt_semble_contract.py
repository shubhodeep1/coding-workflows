#!/usr/bin/env python3
"""Deterministic checks for judge-path Semble prompt wiring."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATE_POLL_PROCESS = REPO_ROOT / "scripts" / "orchestrate_poll_process.sh"
REVIEW_RB_JUDGE = REPO_ROOT / "scripts" / "review_rb_judge.sh"
MODE_JUDGE = REPO_ROOT / "prompts" / "mode-judge.txt"
MODE_REVIEW_BLOCKED = REPO_ROOT / "prompts" / "mode-judge-review-blocked.txt"
MODE_STALL = REPO_ROOT / "prompts" / "mode-judge-stall-recovery.txt"


def test_judge_prompts_keep_read_guidance_and_add_placeholder() -> None:
	mode_judge = MODE_JUDGE.read_text(encoding="utf-8")
	review_blocked = MODE_REVIEW_BLOCKED.read_text(encoding="utf-8")
	stall = MODE_STALL.read_text(encoding="utf-8")
	assert "read them in parallel via the\nread tool." in mode_judge
	assert "{{SEMBLE_PREFETCH}}" in mode_judge
	assert "Use them to inspect the actual code" in review_blocked
	assert "{{SEMBLE_PREFETCH}}" in review_blocked
	assert "Decision goals (ordered):" in stall
	assert "{{SEMBLE_PREFETCH}}" in stall


def test_orchestrate_poll_judge_paths_materialize_prefetch_files() -> None:
	body = ORCHESTRATE_POLL_PROCESS.read_text(encoding="utf-8")
	assert 'source scripts/semble_helpers.sh' in body, (
		"orchestrate_poll_process.sh must source the shared Semble helper for judge additive retrieval."
	)
	assert 'build_judge_semble_prefetch_file()' in body, (
		"orchestrate_poll_process.sh must use one localized helper to build judge-side Semble context."
	)
	assert 'SEMBLE_PREFETCH_FILE="${stall_semble_prefetch_file}" bash scripts/render_prompt.sh prompts/mode-judge-stall-recovery.txt' in body
	assert 'SEMBLE_PREFETCH_FILE="${RB_JUDGE_SEMBLE_PREFETCH_FILE}" bash scripts/render_prompt.sh prompts/mode-judge-review-blocked.txt' in body
	assert 'SEMBLE_PREFETCH_FILE="${JUDGE_SEMBLE_PREFETCH_FILE}" bash scripts/render_prompt.sh prompts/mode-judge.txt' in body
	assert 'judge phase=stall issue=${issue_num}' in body
	assert 'judge phase=review-blocked issue=${rb_issue} pr=${RB_PR}' in body
	assert 'judge phase=wave-completion tracking=${TRACKING_NUM}' in body


def test_review_blocked_judge_uses_shared_helper_and_prefetch_file() -> None:
	body = REVIEW_RB_JUDGE.read_text(encoding="utf-8")
	assert 'source "${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh"' in body or 'source "scripts/semble_helpers.sh"' in body, (
		"review_rb_judge.sh must source the shared Semble helper before building additive judge context."
	)
	assert 'build_review_blocked_semble_block()' in body
	assert 'build_review_blocked_semble_block "${RB_JUDGE_SEMBLE_PREFETCH_FILE}"' in body
	assert 'SEMBLE_PREFETCH_FILE="${RB_JUDGE_SEMBLE_PREFETCH_FILE}" bash "${SUPPORT_SCRIPTS_DIR}/render_prompt.sh" "${SUPPORT_PROMPTS_DIR}/mode-judge-review-blocked.txt"' in body
	assert 'Review-Blocked Judge Context' in body


def main() -> int:
	test_judge_prompts_keep_read_guidance_and_add_placeholder()
	test_orchestrate_poll_judge_paths_materialize_prefetch_files()
	test_review_blocked_judge_uses_shared_helper_and_prefetch_file()
	print("OK: judge prompt Semble contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
