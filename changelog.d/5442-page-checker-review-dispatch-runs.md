<!-- changelog: security -->
- **The Claude PR checker no longer hands a PR to a fixer while its review is still running because other dispatches pushed that review out of view.** This fixes security finding #5442 (`handback-misses-active-review-beyond-first-page`, medium).

`.claude/scripts/check_in_status.py` decides whether a review run is still active before the §26 checker or the catch-all sweep (`scripts/claude_pr_sweep.py`) starts a Claude fixer. For review runs dispatched from the default branch, it read one page of the repository's newest 100 `workflow_dispatch` runs. After 100 newer unrelated dispatches, a live review fell off that page, the checker saw no active run, and it handed the PR to a fixer mid-review. The checker now reads only the two review wrappers' own dispatch runs (`internal-review.yml` and `ai-review.yml`), one listing per active status, page by page up to the listing's `total_count`. A listing it cannot read in full is a read failure: the checker reports `action: retry`, hands nothing back, and reads again at the next check-in.

| The numbers that matter | Value |
| --- | --- |
| Listings read | 1 per review wrapper and active status (`queued`, `in_progress`, and `pending` in hand-back mode), 100 runs per page, at most 10 pages each |
| API calls in a repo with one of the two wrappers | 4 in hand-back mode (3 statuses plus one 404 for the absent wrapper), up from 1, only when no run is active on the PR's head branch |
| Counts as incomplete | a failed or malformed page, a 404 after the wrapper's first read, no `total_count`, more runs than 10 pages hold, fewer distinct run ids than `total_count` |

What this means for operators and consumer repos: after the next `@stable` sync, a `claude/*` PR whose review was dispatched from the default branch keeps waiting while that review runs, however many other workflows were dispatched since. A listing that stays unreadable shows up as repeated `retry` verdicts; the §26 checker then stops renewing the pushing session's 7-day hand-back, and the `/implement-plan-claude` checker routes to a block stage after three retries.

### For contributors

`PR_NAMED_REVIEW_WORKFLOW_RUNS_PATH` is the new per-wrapper, per-status listing, `_pr_named_active_review_run_count` counts matching runs over every wrapper and status, and `_complete_dispatch_run_listing` pages one listing and raises `ReadError` when it is incomplete. `PR_NAMED_REVIEW_RUNS_PATH` and `DISPATCHED_REVIEW_RUNS_PATH` stay defined but are no longer read.
