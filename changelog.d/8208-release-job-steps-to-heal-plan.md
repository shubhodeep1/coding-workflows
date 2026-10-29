<!-- changelog: changed -->
- **Four test steps moved out of the CI job `tests-release-and-log-analysis`.** That job took 15m01s of its 20-minute timeout on `main` (run 40368173231, budget-watch issue #8208). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Mark-stable release-tag refspec contract test` | `tests-heal-plan-and-validation` |
  | `Release CI gate tests (stubbed gh; both release workflows)` | `tests-heal-plan-and-validation` |
  | `Cancelled CI sweep recovery tests` | `tests-heal-plan-and-validation` |
  | `Conflict-dispatch active-run visibility contract tests` | `tests-heal-plan-and-validation` |

  `tests-heal-plan-and-validation` was chosen because it has the most room: its last budget-watch issue was #7690, and no step has moved into it since. The steps were picked by test and subprocess counts, because the run's step timings could not be read. None of them needs git history or Node, which only the release job sets up for its other steps. These durations are estimates, not measurements; the next `budget-watch` run on `main` checks every job. `tests/test_ci_poll_test_sharding.py` now checks that each moved step runs once, in its new job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-heal-plan-and-validation`. Operators have nothing to do.
