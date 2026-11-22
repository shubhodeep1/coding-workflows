<!-- changelog: fixed -->
- **A merge deferred by the merge lease no longer pages "autofix exhausted".** When the review-blocked judge approves `merge_with_followup` but the base freshness gate cannot verify the base, the Telegram alert is now a WARNING that says so, sent once per PR head on runs that reached the autofix iteration limit.

Before this fix, `judge_skip_reason=base_freshness_unknown` (set by `scripts/review_rb_judge.sh`, for example while `MERGE_LEASE ... outcome=wait reason=held` waits on another PR) fell through to the generic case of the "Telegram review-blocked judge decision" step in `.github/workflows/review_autofix.yml`. It paged CRITICAL "PR review blocked — autofix exhausted (5 iterations) Needs human intervention" even though autofix had run only 4 and 2 times (PRs #9502 and #9510), and it paged again on every judge re-fire. The alert now reads "merge_with_followup approved, but base freshness could not be verified (merge lease held or freshness check failed); merge deferred", names the lease holder PR when the judge saw one, and says the merge retries when the lease frees. The review-blocked PR comment (`scripts/review_autofix_step_post_review_blocked_comment.sh`) gets the same accurate text instead of "maximum number of autofix iterations".

| The numbers that matter | Value |
| --- | --- |
| Alert level | CRITICAL to WARNING |
| Alerts and PR comments per PR head on judge re-fire runs | 1 (was one per re-fire) |
| New per-head comment marker | `<!-- rb-judge-base-freshness:v1 head=<sha> -->` |
| New step outputs | `judge_merge_lease_blocker` (rb_judge), `rb_judge_freshness_repeat` (rb_blocked_comment) |

What this means for operators: a PR waiting on the merge lease shows up once as a WARNING with the holder PR, not as a page for a human to fix autofix. If the same head stays deferred, check the lease holder PR; a new push re-arms the alert.

### For contributors

The dedupe reuses the issue #9249 `rb-judge-crash` read in the comment step: one marker is chosen per run (crash first), the newest comment page is read only on those runs, and every read or parse failure posts and pages as before (fail open). A judge crash on the same run still pages CRITICAL through the crash path. Known residual: a run that reaches the judge only because the single-issue security pass is exhausted skips the comment step, so the dedupe does not run there and the WARNING is sent on each same-head re-fire of that kind.
