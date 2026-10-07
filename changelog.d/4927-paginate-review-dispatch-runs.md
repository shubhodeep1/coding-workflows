<!-- changelog: security -->
- **The orchestrator poller no longer misses a live review run because unrelated dispatches crowded it off one page.** Before a review dispatch or a stall-recovery empty-commit push, the poller now reads every review-wrapper dispatch run in the review window, and skips the action when it cannot read them all.

`_pr_named_review_dispatch_runs` in `scripts/orchestrate_poll_process.sh` finds the review runs dispatched from the default branch for a PR (`Internal: AI Review & Autofix [pr:<N>]`, `AI Review [pr:<N>]`). It used to read one page of the newest 100 `workflow_dispatch` runs of every workflow. More than 100 newer dispatches could push a still-active review off that page, and the stall recovery then pushed an empty commit onto the PR head, discarding the in-flight review (security finding `review-run-global-window-exhaustion`, issue #4927). The lookup now lists only `internal-review.yml` and `ai-review.yml` dispatch runs created within `REVIEW_RUN_MAX_RUNTIME_MINUTES`, page by page until it has read every run the listing reports. An incomplete listing (a failed or malformed page, a listing that shifted while being read, or more runs than 10 pages hold) skips the conflict dispatch, the failed-autofix redispatch, and the empty-commit push, and the next poll cycle retries. The failed-autofix redispatch reads back 120 minutes further (`REVIEW_RUN_MAX_RUNTIME_MINUTES + STALL_THRESHOLD_MINUTES`), so a review run that failed at its 240-minute job timeout is still redispatched rather than answered with an empty commit.

| The numbers that matter | Value |
| --- | --- |
| `internal-review.yml` dispatches in coding-workflows, 2026-09-29 | 153 in 5 hours, 701 in 24 hours |
| Window the old single page covered here | under 3.5 hours |
| Lookback window | `REVIEW_RUN_MAX_RUNTIME_MINUTES` (default 250 minutes); 370 minutes for the failed-autofix redispatch |
| Page cap per wrapper | 10 pages of 100 (GitHub's 1,000-result limit) |
| API calls per lookup in coding-workflows | 3 (two `internal-review.yml` pages, one `ai-review.yml` 404), up from 1; 4 for the redispatch lookup |

What this means for operators: a stall recovery that cannot see every recent review dispatch now waits a cycle instead of pushing. Search the poller log for `PR_NAMED_REVIEW_RUNS pr=<N> outcome=incomplete` and `STALL_INFLIGHT_DIRECT_CHECK … outcome=pr_named_listing_incomplete` to see why a push or dispatch was held back.

### For contributors

The helper keeps its stdout contract (a JSON array of matching runs, newest first) and now returns 1 when the listing is incomplete. `_direct_inflight_review_run_on_branch` prints the sentinel `listing-incomplete` in that case, and both empty-commit push sites treat it as a skip under the existing `retrigger_review_skipped_inflight` action. A wrapper the repo does not have answers 404 and counts as complete and empty. The merge train's `_mt_inflight_review_branches` and the sweep's snapshot are unchanged. The poller now strips leading zeros from `STALL_THRESHOLD_MINUTES` and `REVIEW_RUN_MAX_RUNTIME_MINUTES` at startup. Its `^[0-9]+$` check accepted them, and bash arithmetic then read `0250` as octal or failed on `08`.
