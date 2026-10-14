<!-- changelog: fixed -->
- **The review-blocked judge now acts only on the pull request head it evaluated.**

Previously, a push during the judge call could let the `fix` path post the head-gate success status and apply `ai:ready-to-merge` for a head the judge never saw, or let a reissue keep unjudged code as its baseline. Now `scripts/review_rb_judge.sh` compares the live PR head with the head named in the judge prompt, and skips every action before any comment, label, status or close when the head moved (`judge_skip_reason=stale_head`, log line `RB_JUDGE_STALE_HEAD pr= judged_head= live_head= action=`). When the live head cannot be read, `fix` and `close_and_reissue` are refused (`judge_skip_reason=head_unverified`), and so is a merge that open security findings convert to `fix`. Other merge actions are unchanged, because `--match-head-commit` already binds them.

What this means for operators: a review-blocked PR that receives a push while the judge is running is left for a later review or judge run, and its linked issue no longer reaches `ai:ready-to-merge` from a verdict on an older head.

### For contributors

The live head comes from the PR read the script already makes, so no API call is added. A force-push back to an older commit counts as moved. The one exception is the head this run checked out (`INITIAL_HEAD_SHA`), sitting behind the run's own unpushed editor or `[ai-merge-resolve]` commit, because the judge runs before "Push all pending commits". A spot-fix reissue uses the evaluated head as its baseline, which removes one `gh pr view` call. When the evaluated head is the run's own unpushed commit, the reissue uses the live PR head instead, because `implement.yml` ignores a baseline that differs from the closed PR's `headRefOid`. Tests: `tests/test_review_rb_judge_head_binding.py`.
