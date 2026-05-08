#!/usr/bin/env python3
"""Deterministic checks for reviewer-prompt Semble wiring.

These tests pin the new reviewer-path Semble block as additive context:

1. scripts/review_run_reviewers.sh must source the shared helper and append
   the reviewer Semble block after the existing diff/pre-bundle context.
2. Fallback/log text must remain out of the prompt body; only the explicit
   `=== SEMBLE: ... ===` block belongs in prompt stdout.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_RUN_REVIEWERS = REPO_ROOT / "scripts" / "review_run_reviewers.sh"


def test_reviewer_prompt_uses_shared_semble_helper_additively() -> None:
	body = REVIEW_RUN_REVIEWERS.read_text(encoding="utf-8")
	assert "source \"${SUPPORT_SCRIPTS_DIR}/semble_helpers.sh\"" in body or "source \"scripts/semble_helpers.sh\"" in body, (
		"Reviewer prompt assembly must source the shared Semble helper instead of inlining ad-hoc query logic."
	)
	assert 'build_reviewer_semble_block()' in body, (
		"Reviewer prompt assembly must build the Semble block through a dedicated helper."
	)
	last_commit_idx = body.index('=== END ${LAST_COMMIT_STAT_FILE} ===')
	semble_idx = body.index('$(build_reviewer_semble_block)')
	comments_idx = body.index('=== BEGIN UNTRUSTED ${PR_ALL_COMMENTS_CONTEXT_FILE}')
	assert last_commit_idx < semble_idx < comments_idx, (
		"Reviewer Semble block must be appended after the existing diff/pre-bundle context and before the later untrusted comment sections."
	)


def test_reviewer_prompt_semble_block_keeps_logs_out_of_prompt() -> None:
	body = REVIEW_RUN_REVIEWERS.read_text(encoding="utf-8")
	assert "SEMBLE_QUERY" not in body[body.index('cat > "${REVIEWER_PROMPT_BODY_FILE}" <<__REVIEWER_PROMPT__'):body.index('__REVIEWER_PROMPT__', body.index('cat > "${REVIEWER_PROMPT_BODY_FILE}" <<__REVIEWER_PROMPT__') + 1)], (
		"Reviewer prompt body must not inline Semble log tokens; only the rendered Semble block belongs in prompt stdout."
	)
	assert '"Reviewer Context"' in body, (
		"Reviewer Semble block should use a stable header label for deterministic prompt shape."
	)


def main() -> int:
	test_reviewer_prompt_uses_shared_semble_helper_additively()
	test_reviewer_prompt_semble_block_keeps_logs_out_of_prompt()
	print("OK: reviewer prompt Semble contract assertions hold")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
