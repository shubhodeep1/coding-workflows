<!-- changelog: fixed -->
- **Approved PRs now take turns updating from their base, and a PR whose final CI passes after the merge step stopped waiting is merged on the next poll tick.** Before this, approved PRs were updated from `main` up to six times without merging, and green integration children sat unmerged.

Each base branch now has one merge lease. Only the PR holding it may update its branch from the base and run the combined-tree CI. Other approved PRs that need an update wait under `ai:merge-lease-wait` instead of each starting a CI run that the next merge makes stale. A PR that needs no update still merges at once, unless it changes a file the lease holder changes. The merge-train release step, on every poll tick and every PR close, frees a lease whose holder closed or expired and hands it to the next waiter. Priority labels come first, then the lowest PR number. When a merge step's 45-minute checks wait times out, the head is marked with an `ai-review/final-ci-merge-pending` status, and the same release step enables head-bound auto-merge as soon as that head's final CI is green.

| The numbers that matter | Value |
| --- | --- |
| Most updates seen on one approved PR before this change | 6 (#9367) |
| Final CI run time on the congested pool, 2026-11-17 | 1.5 to 2.5 hours |
| Merge-step checks wait (`AUTO_MERGE_CHECKS_WAIT_MINUTES`) | 45 minutes |
| Lease expiry without renewal (`MERGE_LEASE_TTL_MINUTES`) | 240 minutes |
| Release step cadence | every poll tick (15 min) and every PR close |
| New jobs or workflows | 0 |

What this means for operators: expect fewer `Branch updated from main before merging` comments and fewer final CI runs per merged PR. Waiting PRs show `ai:merge-lease-wait`; PRs waiting on their final CI show `ai:final-ci-merge-pending`. `MERGE_LEASE_ENABLED=false` and `FINAL_CI_MERGE_RESUME_ENABLED=false` turn the two parts off. A lease read or write failure logs `MERGE_LEASE ... outcome=unavailable`, and the merge then behaves as it did before.

### For contributors

The lease record is a commit on `refs/merge-lease/<base>`, changed only by fast-forward PATCH, so two writers that read the same record cannot both win. The resume trusts only a status that the pipeline login created on the exact head SHA, together with a pipeline `ai-review/head-gate` success. Code: `_merge_lease_gate` and `_pr_final_ci_merge_pending_mark` in `scripts/pr_checks_lib.sh`, and `_mt_merge_lease_tick` and `_mt_final_ci_merge_resume` in `scripts/review_merge_train.sh`. Tests: `tests/test_merge_lease.py`.
