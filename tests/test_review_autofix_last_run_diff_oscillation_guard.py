#!/usr/bin/env python3
"""Contract test for the LAST_RUN_DIFF generation in `review_autofix.yml`.

The downstream OSCILLATION GUARD in the editor prompt
(`scripts/review_apply_fixes.sh:441-462`) tells the model to leave any hunk
visible in `last_run_diff.patch` alone unless it can attach a "regression
fingerprint" + "runtime failure path". This guardrail is correct ONLY when
the diff actually reflects prior AI autofix work; feeding it the diff of
non-autofix commits (impl commits, smoke-test bait commits, manual pushes)
falsely trips the guard and causes the editor to silently bail out.

That regression was observed on review_autofix run 25254574828 (smoke gate
of release v1.1.0): the bait commit was at HEAD, the unconditional
`git diff HEAD~1...HEAD` fed the bait line into `last_run_diff.patch`,
the editor at `reasoning=low` produced empty output across 6 attempts
(3 + 3 retry), and Phase 4b's `bait_remained` verification correctly
failed the smoke gate. The fix narrows the diff source to the most recent
`[ai-autofix]`/`[judge-fix]` commit, matching the existing prefix
convention used by `scripts/review_apply_fixes.sh:2838` and the
`Count autofix iterations` step at line 2096-2105.

Three invariants must hold for the OSCILLATION GUARD not to false-trip:

1. The diff source must be a walk-back to the most recent
   `[ai-autofix]`/`[judge-fix]` commit (awk regex on `git log`), NOT an
   unconditional `git diff HEAD~1...HEAD`.
2. The fall-through placeholder must start with "No previous AI autofix"
   so `scripts/review_run_reviewers.sh:249`'s sentinel-detection regex
   correctly classifies the run as the first iteration.
3. The diff range must use `${SHA}^...${SHA}` so the autofix commit's own
   diff is shown (not whatever HEAD~1 happens to be).
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEW_AUTOFIX = REPO_ROOT / ".github" / "workflows" / "review_autofix.yml"


def _review_autofix_text() -> str:
	return REVIEW_AUTOFIX.read_text(encoding="utf-8")


def _generate_diff_block(text: str) -> str:
	"""Return the body of the `Generate diff context` step."""
	marker = "- name: Generate diff context"
	start = text.find(marker)
	assert start != -1, "Missing `Generate diff context` step in review_autofix.yml"
	next_step = text.find("\n      - name:", start + len(marker))
	assert next_step != -1, "Could not bound the `Generate diff context` step"
	return text[start:next_step]


def test_last_run_diff_walks_back_to_autofix_commit() -> None:
	"""The diff source must be the most recent `[ai-autofix]`/`[judge-fix]`
	commit, not an unconditional `HEAD~1...HEAD` diff. Without this walk,
	the smoke gate's bait commit (or any non-autofix commit at HEAD)
	leaks into `last_run_diff.patch` and false-trips the OSCILLATION
	GUARD downstream."""
	block = _generate_diff_block(_review_autofix_text())
	assert "LAST_AUTOFIX_SHA=" in block, (
		"`Generate diff context` step is missing the LAST_AUTOFIX_SHA "
		"walk-back. Reverting to unconditional `git diff HEAD~1...HEAD` "
		"re-introduces the run-25254574828 OSCILLATION GUARD false-trip."
	)
	assert r"$2 ~ /^\[(ai-autofix|judge-fix)\]/" in block, (
		"LAST_AUTOFIX_SHA walk must filter on the "
		"`[ai-autofix]`/`[judge-fix]` commit-subject prefix. The same "
		"regex is used by scripts/review_apply_fixes.sh:2838 — keep them "
		"in lockstep."
	)


def test_last_run_diff_uses_caret_dotdotdot_range() -> None:
	"""The diff range must be `${SHA}^...${SHA}` so it shows the autofix
	commit's own changes against its parent. A range against HEAD would
	leak any post-autofix commits (manual pushes, bait) back into the
	diff and re-introduce the OSCILLATION GUARD false-trip."""
	block = _generate_diff_block(_review_autofix_text())
	assert '"${LAST_AUTOFIX_SHA}^...${LAST_AUTOFIX_SHA}"' in block, (
		"LAST_RUN_DIFF must diff `${SHA}^...${SHA}` to scope the diff to "
		"the autofix commit alone. Any other range can leak unrelated "
		"commits into the editor's OSCILLATION GUARD context."
	)


def test_last_run_diff_no_unconditional_head1_diff() -> None:
	"""The unconditional `git diff HEAD~1...HEAD` must not be present.
	That was the regression-source: it fed the smoke gate's bait commit
	(authored as `[E2E Smoke Test] inject editor bait line ...`, NOT
	`[ai-autofix]`) into the editor's OSCILLATION GUARD context."""
	block = _generate_diff_block(_review_autofix_text())
	assert 'git diff "HEAD~1...HEAD"' not in block, (
		"`Generate diff context` step still has the unconditional "
		'`git diff "HEAD~1...HEAD"` for LAST_RUN_DIFF. This is exactly '
		"the regression that caused review_autofix run 25254574828 to "
		"silently no-op against the smoke-test bait commit."
	)


def test_last_run_diff_placeholder_matches_first_iteration_sentinel() -> None:
	"""The fall-through placeholder must begin with "No previous AI autofix"
	so `scripts/review_run_reviewers.sh:249`'s
	`^(No previous AI autofix|Initial run — no previous commit)` regex
	correctly flips IS_FIRST_ITERATION=true. Without this, reviewers get
	the iteration-2 prompt on a fresh smoke PR, which is also wrong."""
	block = _generate_diff_block(_review_autofix_text())
	assert '"No previous AI autofix run on PR head" > "${LAST_RUN_DIFF_FILE}"' in block, (
		"LAST_RUN_DIFF_FILE placeholder must start with `No previous AI autofix` "
		"so review_run_reviewers.sh's first-iteration sentinel regex matches."
	)
	# The stat-file sentinel must remain intact for downstream consumers.
	assert '"FIRST_RUN_NO_PREVIOUS_AI_CHANGES" > "${LAST_RUN_DIFF_STAT_FILE}"' in block, (
		"LAST_RUN_DIFF_STAT_FILE sentinel `FIRST_RUN_NO_PREVIOUS_AI_CHANGES` "
		"is part of the cross-script contract and must be preserved."
	)


if __name__ == "__main__":
	test_last_run_diff_walks_back_to_autofix_commit()
	test_last_run_diff_uses_caret_dotdotdot_range()
	test_last_run_diff_no_unconditional_head1_diff()
	test_last_run_diff_placeholder_matches_first_iteration_sentinel()
	print("All LAST_RUN_DIFF OSCILLATION GUARD contract tests passed.")
