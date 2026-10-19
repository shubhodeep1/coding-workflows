<!-- changelog: changed -->
- **Six test steps moved out of the CI job `tests-promote-stall-and-review`.** That job took 16m06s of its 20-minute timeout on `main` (run 39216253741, budget-watch issue #7677). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names:

  | Step | New job |
  |---|---|
  | `Clarify loop guard unit tests` | `tests-heal-plan-and-validation` |
  | `Implement files_touched scope guard unit tests` | `tests-heal-plan-and-validation` |
  | `Shared PR check-runs merge gate (pr_checks_lib) tests` | `tests-heal-plan-and-validation` |
  | `Merge-base freshness gate (pr_checks_lib) tests` | `tests-heal-plan-and-validation` |
  | `Promote cycle and blocked-verdict unit tests` | `tests-release-and-log-analysis` |
  | `Review-blocked judge label propagation contract test` | `tests-release-and-log-analysis` |

  The step timings could not be read when this was planned. The steps were therefore chosen by estimated cost: test count, weighted up for tests that start a subprocess. Together they make up about 40% of the job's estimated cost. Most of that went to the heal job, because the release job was the most recent job to hit its timeout (#7560). Neither receiving job's current duration has been measured; the next `budget-watch` run on `main` checks all three jobs.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under its new job's check-run. Operators have nothing to do.
