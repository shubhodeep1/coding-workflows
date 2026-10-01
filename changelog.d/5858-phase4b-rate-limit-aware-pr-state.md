<!-- changelog: fixed -->
- **The stable release gate's Phase 4b now waits out GitHub rate limits instead of failing as if GitHub were down.**

In `.github/workflows/test-and-mark-stable.yml`, step `Phase 4b: Verify editor restored canary (pytest + retry)` watches the PR while the adopted review run finishes. Before this fix, a rate-limited PR-state read counted the same as an unreadable PR. On 2026-10-01, four such reads in a row ended release run 36797692597 with `pr_state_check_failed` about 5 minutes before the 25-minute retry deadline, while the review run was still `in_progress`. Now the step recognises a rate-limited read and does not count it toward the four-failure breaker. It waits for the limit to reset, using `GET /rate_limit` for a spent quota or 60 seconds for a secondary limit, then keeps polling the review run. A rate-limited read of the review run's status, or of the run list while a dispatched run registers, waits the same way instead of re-reading every 15 seconds. The read after a wait happens at once, even when the wait ran up to a deadline, so a limit that clears at the last moment still gets its read. After a wait on a rate-limited PR-state read, the step reads the PR state again before it looks at the review run, so a PR closed during the wait still ends the step with `pr_closed_during_retry`. A completed review run moves the step on to attempt 2 only after a PR-state read in the same poll that was not rate-limited, so a PR whose state stays rate-limited is never treated as still open. The wait never runs past the retry deadline, and an expired deadline still fails the step with `retry_timeout`.

| The numbers that matter | Value |
| --- | --- |
| Failed release run | 36797692597 (issue #5858) |
| Wait after a rate-limited read | until `core.reset` + 1 s, or 60 s for a secondary limit |
| Retry deadline (`EDITOR_RETRY_BUDGET_MINUTES`) | 25 minutes, unchanged |
| Breaker for other unreadable PR states | 4 in a row, unchanged |
| Short retries on a rate-limited read | none (was 3 attempts, 2 s and 4 s apart) |

What this means for operators: a release gate that runs into a GitHub rate limit while Phase 4b waits now finishes verifying the canary when the limit resets in time. If it does not reset in time, the gate reports `retry_timeout`, and the step log says how many reads were rate-limited and whether each wait came from the quota's reset time or the 60-second fallback.

### For contributors

`gh_api_with_retry` in that step returns `GH_API_RATE_LIMITED_RC` (75) for a rate-limited read, and every existing `if !` / `||` caller still treats it as a failure. `fetch_pr_state` reports `rate_limited`. `tests/test_test_and_mark_stable_review_blocked_budget.py` runs the step's own retry block in bash with a stub `gh` and a fake clock, and covers the recovery, rate-limited run-status and run-list reads, the deadline and registration-window expiry, PR closure (including a closure during a wait while the run completes), a completed run while the PR state stays rate-limited, and the unchanged breaker.
