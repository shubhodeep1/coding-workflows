<!-- changelog: security -->
- **The pending-checks auto-merge no longer races a newer review of the same PR.** The hourly `claude-pr-catch-all` sweep now waits while any newer review of the PR may still be running, and never merges when the latest newer review did not succeed.

The sweep's pending-checks pass (issue #4900) enabled auto-merge from an earlier clean review's `ai:claude-fixer-pending-checks:v1` marker as soon as the head's checks were green. A forced review (the `force-review` label, `[force-review]` in the title, or a `force_rb_judge` dispatch) runs the full reviewer panel on that head from the default branch. Its check runs attach to the default branch's commit, so the PR head still looked ready, and the sweep could enable auto-merge before that review reported its findings. Nothing disables auto-merge afterwards, so with green checks the PR merged within seconds. `scripts/claude_fixer_pending_checks.py` now lists the PR's review runs first. It waits (`review_active`) while any run on the head branch, an `internal-review.yml` dispatch titled `[pr:<N>]`, or any `review_autofix.yml` / `ai-review.yml` dispatch has not completed. It refuses (`review_superseded`) when the latest completed review of the PR newer than the marker's run did not conclude `success`, or when the marker is no longer the live one on a re-read of the comments taken after those run reads.

| The numbers that matter | Value |
| --- | --- |
| Audit finding | `pending-merge-races-newer-review`, high, `scripts/claude_pr_sweep.py:253` (issue #5148) |
| Extra reads per PR that is ready to merge | 1 head-branch runs listing, 3 `workflow_dispatch` runs listings (a missing workflow costs its one 404), 1 comments re-read |
| Extra reads for every other PR | 0 |
| Longest added delay | one sweep tick (`17 * * * *`) per active review |

What this means for operators: a PR someone forces a new review on is not merged from the earlier clean result. It merges on the next hourly tick after that review finished cleanly, or goes to a Claude fixer when that review hands off findings. `review_autofix.yml` and `ai-review.yml` dispatches carry no PR in their run name, so any one of them still running delays every pending-checks merge in that repository by one tick. The sweep logs `pending_checks ... state=review_active` or `state=review_superseded` with the run it waited on.

### For contributors

The new check is `check_review_runs()` in `scripts/claude_fixer_pending_checks.py`, called from `evaluate()` after the marker's own review run is verified. It reuses `FIXER_WORKFLOW_PATHS`, `DISPATCHED_REVIEW_TITLE`, and `DISPATCHED_REVIEW_RUNS_PATH` from `.claude/scripts/check_in_status.py`, which is unchanged. Any status other than `completed` counts as active. `tests/test_claude_fixer_pending_checks.py` covers the audit's scenario, every active and superseded path, the settled cases that still merge, and the PR #4869 sequence with a forced review in between.
