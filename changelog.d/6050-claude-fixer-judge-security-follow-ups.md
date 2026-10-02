<!-- changelog: security -->
- **The Claude-fixer GPT judge no longer merges past a security finding it did not classify, and a past "invalid" ruling no longer hides a new finding on nearby lines.** Both were raised by the security audit of the Claude-fixer convergence project (#6050, #6051).

At the judge-fix cap (`CLAUDE_FIXER_JUDGE_FIX_CAP`, default 2), an upheld finding the model gave no ruling for, or upheld without a valid category, used to count as `other` and let the pull request merge with a follow-up issue. Such a finding could be a security finding the model left out, so the judge now holds the pull request for a human (`ai:needs-human`, reason `upheld_unruled_or_uncategorized_at_cap`). Sticky rulings, which keep a finding the judge ruled invalid from blocking later rounds, now also require the new finding to make the same claim as the ruled one, compared case- and whitespace-insensitively. A different defect reported within three lines of a rejected finding stays blocking.

| The numbers that matter | Value |
| --- | --- |
| Sticky-ruling match | same file, start line within 3 lines, same claim |
| New hold reason | `upheld_unruled_or_uncategorized_at_cap` |

What this means for operators: a `claude/*` pull request can no longer auto-merge on a judge run that skipped a finding at the cap; expect an `ai:needs-human` hold instead. Re-reported findings whose wording changes between rounds are no longer demoted, so a round can need one more judge pass to converge.

### For contributors

`scripts/review_claude_fixer_judge.py`: `decide` tracks upheld findings without a ruling or category; `sticky` compares `_claim_key` of the ledger bullet (its `PROBLEM:` line, else its header) with the ruling's stored `claim`. Tests: `tests/test_review_rb_judge_claude_mode.py`, `tests/test_review_autofix_claude_fixer_mode.py`.
