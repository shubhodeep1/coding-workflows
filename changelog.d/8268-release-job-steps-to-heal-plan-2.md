<!-- changelog: changed -->
- **Four more test steps moved out of the CI job `tests-release-and-log-analysis`.** That job took 15m13s of its 20-minute timeout on `main` (run 40716905879, budget-watch issue #8268). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Set up Node 22 for the claude-pool-broker Worker tests` | `tests-heal-plan-and-validation` |
  | `claude-pool-broker Worker tests (vitest)` | `tests-heal-plan-and-validation` |
  | `Workflow log analysis contract tests` | `tests-heal-plan-and-validation` |
  | `Semantic cache unit tests` | `tests-heal-plan-and-validation` |

  `tests-heal-plan-and-validation` was chosen because #8246 split its largest step into 4 parallel processes, so it is the job most likely to have room. The run's step timings could not be read, so the steps were picked by estimated cost, not measured; the next `budget-watch` run on `main` checks every job. The Node setup and the vitest step move together, in that order, and none of the four needs git history. `tests/test_ci_poll_test_sharding.py` now checks that each moved step runs once, in its new job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-heal-plan-and-validation`. Operators have nothing to do.
