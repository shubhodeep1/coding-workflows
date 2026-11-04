<!-- changelog: fixed -->
- **The review autofix pre-push gate no longer throws away the editor's whole change over lint findings.** A repair turn that undid every edit to clear new `ruff` or `shellcheck` findings now has that undo reverted, so the editor's change is committed and the findings show in the gate summary.

The "Pre-push affected-test gate" step of `review_autofix.yml` checks the editor's change against the PR head. When a reviewer asked the editor to restore code the PR base already had, and that code carried a lint finding (PR #8747: three continuation lines with mixed tabs and spaces, ruff `E101`), the gate counted the finding as caused by the change. The one repair turn then put the file back to the PR head, so the commit step found nothing to commit. Every run on the PR ended in "Editor changes lost", and after the one re-dispatch per head the run posted "Editor changes lost (retry unavailable)" and blocked auto-merge. Now, when every failure caused by the change is a lint unit and the repair leaves no change at all, the gate restores the pre-repair tree, logs `PREPUSH_TEST_GATE phase=review outcome=repair_reverted reason=lint_only_change_erased`, and reports `Repair turn: reverted (it removed the whole change for lint findings only)` with the original `fail` result.

| The numbers that matter | Value |
| --- | --- |
| Runs on PR #8747 that lost the editor's change this way | 3 (41632525262, 41636480237, 41640587443) |
| Lint units that qualify | `ruff`, `shellcheck` |
| Gate re-runs after a reverted repair | 0 (the first result is reported) |

What this means for operators: a PR stuck in an "Editor changes lost" loop on whitespace or lint-only reviewer requests now converges. A repair that fixes lint findings and keeps some of the change is unchanged, and so is a repair that undoes the change for a failing test.

### For contributors

The check is the `lint_only RESULT` mode of the step script's embedded helper in `scripts/review_autofix_step_prepush_test_gate.sh`, called from `review_prepush_test_gate_repair` after the syntax check. `result.json` comes from the container, so a forged lint-only result can at most keep the editor's own pre-repair tree, which is what the gate pushes when it skips the repair. Tests: `tests/test_review_prepush_test_gate.py` (`test_lint_only_repair_that_erases_the_change_keeps_the_editor_edit` and two neighbours).
