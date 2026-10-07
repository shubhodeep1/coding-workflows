<!-- changelog: fixed -->
- **The review editor's output is no longer rejected when it shortens the audit count labels.** Every `claude/*` PR reviewed with the Claude editor since 2026-10-06 was failing all three editor attempts and ending `ai:review-blocked` after the identical-failure cap.

The editor summary's `Review file issue audit:` bullets carry four counts per reviewer file. `scripts/review_apply_fixes.sh` only accepted the full labels (`total issues listed`, `issues applied`, `issues already applied`, `issues ignored`), and the Claude editor writes `total 5; applied 0; already applied 0; ignored 5`. Each attempt was rejected as a format failure, the run finalized with "editor no-op suspicious", and the third identical round tripped the fingerprint cap (PR #6605, run 37565800725; the same fingerprint on #6606 and #6614). The prompt now shows the exact bullet shape with the full labels, and both validators (`review_apply_fixes.sh` and `scripts/validate_editor_audit.sh`) accept the short labels as aliases, with the arithmetic check unchanged.

| The numbers that matter | Value |
| --- | --- |
| Editor attempts rejected per review round | 3 of 3 |
| Pool quota spent per rejected round on #6605 | about 2M cached input tokens per attempt |
| Rounds before the identical-failure cap labels the PR | 3 |

What this means for operators: PRs that were stuck on the `editor no-op suspicious` comment with fingerprint `29efdb47…` complete their editor round on the next review run. A push or a review re-dispatch starts that run.

### For contributors

Tests: `tests/test_review_apply_fixes_reviewer_manifest_validation.py` (`test_short_count_labels_from_claude_editor_are_accepted`, `test_short_labels_still_need_all_four_counts`, `test_prompt_spells_out_the_audit_bullet_shape`) and `tests/test_validate_editor_audit.py` (`test_short_labels_from_claude_editor_balance`, `test_short_labels_mismatch_still_fails`).
