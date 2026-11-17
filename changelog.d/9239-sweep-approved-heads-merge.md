<!-- changelog: fixed -->
- **Approved PRs on branches older than the final-CI trigger can merge, and the review sweep stops re-reviewing approved heads it cannot merge.** A merge wait whose final CI cannot be started now ends at once instead of holding a runner for 45 minutes.

A branch whose `ci.yml` predates the `workflow_dispatch` trigger cannot be given its final CI run, so the required `lint` never appears. PR #9579, approved and `clean`, logged `REVIEW_FINAL_CI ... reason=dispatch_refused` and then waited out `AUTO_MERGE_CHECKS_WAIT_MINUTES` on every review run. The wait in `scripts/pr_checks_lib.sh` now stops at once with `reason=final_ci_dispatch_failed`. When the base's `ci.yml` has the trigger and the freshness check found the approved head `clean`, it updates the branch from its base once, with the same verified, marker-bound update the merge-base freshness gate makes, and dispatches the update head's final CI. `review_autofix_sweep.yml` now reads every open PR's `ai-review/head-gate` state in one GraphQL call per 100 PRs. It skips the review dispatch for an approved head whose final CI failed (PR #9239 had 40 gate statuses on one head; the sweep's base-sync pass handles those heads), and the final-CI safety net checks approved heads first instead of spending its cap on gate-pending ones.

| The numbers that matter | Value |
| --- | --- |
| Runner time held per refused final-CI dispatch (before) | 45 min (`AUTO_MERGE_CHECKS_WAIT_MINUTES`) |
| Sweep tick 2026-11-17 08:30, dispatches to approved heads | 5 of 15 |
| Final-CI sweep at the same tick | `candidates=42 checked=10 gate_not_success=10 ensured=0` |
| New API cost per sweep tick | 1 GraphQL call per 100 open PRs, plus 1 runs read per approved head about to be dispatched |
| New repo variables | `SWEEP_SKIP_APPROVED_FINAL_CI_FAILED`, `FINAL_CI_REFUSED_BASE_UPDATE_ENABLED` (both default `true`) |

What this means for operators: old branches get one automatic base update and then merge on their final CI, and fewer review runs queue on the shared runner pool. Set `SWEEP_SKIP_APPROVED_FINAL_CI_FAILED=false` to have the sweep re-review approved heads with a failed final CI again, or `FINAL_CI_REFUSED_BASE_UPDATE_ENABLED=false` to stop the base update.
