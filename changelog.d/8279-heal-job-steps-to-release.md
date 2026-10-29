<!-- changelog: changed -->
- **Four test steps moved out of the CI job `tests-heal-plan-and-validation`.** That job took 15m06s of its 20-minute timeout on `main` (run 40750326439, budget-watch issue #8279). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Validate process cross-cycle escalation unit tests` | `tests-release-and-log-analysis` |
  | `Activation verification tests (port P4)` | `tests-release-and-log-analysis` |
  | `Clarify loop guard unit tests` | `tests-release-and-log-analysis` |
  | `Implement files_touched scope guard unit tests` | `tests-release-and-log-analysis` |

  `tests-release-and-log-analysis` was chosen because it has the most room: budget-watch issues #8208, #8245 and #8268 each moved four steps out of it (#8268 was its last issue), and #8268 fixed the collector test whose step took about 5 minutes there, so that step now runs in seconds. The steps were picked by test and subprocess counts, because the run's step timings could not be read. None of them needs Node or the prepared `.claude/` live copies. These durations are estimates, not measurements; the next `budget-watch` run on `main` checks every job. `tests/test_ci_poll_test_sharding.py` now checks that each moved step runs once, in its new job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-release-and-log-analysis`. Operators have nothing to do.
