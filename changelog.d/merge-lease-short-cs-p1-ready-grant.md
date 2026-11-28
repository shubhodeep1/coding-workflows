<!-- changelog: changed -->
- **The merge lease now goes to a PR that can merge now, and a holder that conflicts with its base gives up its slot once.** Before, the tick granted the lease on the review head-gate alone, so a PR whose `ci/final` was still pending, or that conflicted with its base, could hold the lease for 4 to 5 hours while ready PRs waited.
  - A waiter is granted only when its newest `ci/final` status is `success` (when `REVIEW_FINAL_CI_WORKFLOW` is set) and GitHub does not report it as unmergeable. A waiter that is not ready keeps `ai:merge-lease-wait` and logs `MERGE_LEASE ... outcome=skipped reason=final_ci_pending|not_mergeable`. Its final CI is dispatched once when its head has none.
  - If no waiter is ready and the lease has been free for `MERGE_LEASE_IDLE_MINUTES`, the first eligible waiter is granted as before (`reason=ready_fallback`), so merges never stall.
  - A holder that conflicts with its base while another waiter is ready is released (`reason=released_conflict`) and labelled `ai:merge-lease-conflict-released`. It returns once its head changes and is never released for a conflict a second time.
  - New repository variables, all optional: `MERGE_LEASE_READY_GRANT_ENABLED` and `MERGE_LEASE_CONFLICT_RELEASE_ENABLED` (default `true`; set both to `false` to roll back), and `MERGE_LEASE_CONFLICT_RELEASED_LABEL`. Read failures count as ready. Operators have nothing to do.

Refs #10098, Refs #10097.
