<!-- changelog: fixed -->
- **Approved PRs now take turns updating from their base branch.** Before this, an approved PR could be updated from its base up to six times without merging, because every other merge made its CI run stale.

Each base branch now has one merge lease. Only the PR holding it may update its branch from the base and run CI on the combined tree. Other approved PRs that need an update wait under `ai:merge-lease-wait` instead of each starting a CI run that the next merge makes stale. A PR that needs no update still merges at once, unless it changes a file the lease holder changes. The merge-train release step, on every poll tick and every PR close, frees a lease whose holder closed or expired and hands it to the next waiter. Priority labels come first, then the lowest PR number.

| The numbers that matter | Value |
| --- | --- |
| Most updates seen on one approved PR before this change | 6 (#9367) |
| Lease expiry without renewal (`MERGE_LEASE_TTL_MINUTES`) | 240 minutes |
| Longest total hold (`MERGE_LEASE_MAX_HOLD_MINUTES`) | 720 minutes |
| Release step cadence | every poll tick (15 min) and every PR close |
| New jobs or workflows | 0 |

What this means for operators: expect fewer `Branch updated from main before merging` comments and fewer CI runs per merged PR. Waiting PRs show `ai:merge-lease-wait`. `MERGE_LEASE_ENABLED=false` turns the lease off. A lease read or write failure logs `MERGE_LEASE ... outcome=unavailable`, and the merge then behaves as it did before.

### For contributors

The lease record is a commit on `refs/merge-lease/<base>`, changed only by fast-forward PATCH, so two writers that read the same record cannot both win. Code: `_merge_lease_gate` in `scripts/pr_checks_lib.sh` and `_mt_merge_lease_tick` in `scripts/review_merge_train.sh`. Tests: `tests/test_merge_lease.py`. On `main`, #9660 also shipped a final-CI merge resume; it depends on this repository's final CI and is not part of the stable line.
