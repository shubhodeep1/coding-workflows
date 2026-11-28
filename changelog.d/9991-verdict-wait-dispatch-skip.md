<!-- changelog: changed -->
- **Heads that already have a verdict and only wait to merge are no longer re-reviewed or re-judged (Refs #9991, #10243).**

On 2026-11-23, 181 of 218 review runs were dispatched re-reviews, and PR #9984 got 12 of them on an unchanged head that only waited for a required check. Stall recovery also re-ran the review-blocked judge on heads it had already decided `merge` on and that waited for the merge lease (#9573 x21, #9637 x20).

The review sweep now skips a head whose `ai-review/head-gate` is success and that GitHub reports mergeable (not conflicted) when it has auto-merge enabled, carries `ai:final-ci-merge-pending`, or has a final CI run in flight (`AUTOFIX_SWEEP_SKIP ... reason=verdict_wait`). The round cap no longer labels a judge-decided head `ai:needs-human`. Stall recovery re-runs the judge only when the head, the lease (the wait label removed), a check result, or the base under the PR's files changed since the decision (`STALL_SKIP ... reason=rb_judge_verdict_wait`). The judge's `ai-review/judge-merge-pending` status now records the decision-time base. A head that waits longer than `VERDICT_WAIT_ALERT_HOURS` raises one Telegram WARNING naming the blocker.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls | none in the sweep loop (same GraphQL query); stall: one GraphQL read in place of a judge dispatch, plus a compare and a files read only when the base moved |
| New repo variables | `VERDICT_WAIT_DISPATCH_SKIP_ENABLED` (default `true`; `false` restores the old behaviour), `VERDICT_WAIT_ALERT_HOURS` (default `6`; `0` off) |
| New jobs, workflows or runs | none; fewer review and judge runs |

What this means for operators: a long-waiting head now raises one alert instead of a review run every 30 minutes. Each existing judge status has no recorded base, so each such PR is judged once more, then the skip applies.
