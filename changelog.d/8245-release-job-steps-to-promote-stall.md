<!-- changelog: changed -->
- **Four more test steps moved out of the CI job `tests-release-and-log-analysis`.** That job took 15m10s of its 20-minute timeout on `main` (run 40625105162, budget-watch issue #8245). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Test-and-mark-stable plan polling guard regression tests` | `tests-promote-stall-and-review` |
  | `Release smoke gate run-lookup regression tests` | `tests-promote-stall-and-review` |
  | `Consumer secrets propagation contract tests` | `tests-promote-stall-and-review` |
  | `Review autofix sweep stale-queued guard tests` | `tests-promote-stall-and-review` |

  `tests-promote-stall-and-review` was chosen because it has the most room: its last budget-watch issue was #7677, and more steps have moved out of it since than into it. The steps were picked by test and subprocess counts, because the run's step timings could not be read. None of them needs git history or Node, which the new job does not set up. These durations are estimates, not measurements; the next `budget-watch` run on `main` checks every job. `tests/test_ci_poll_test_sharding.py` now checks that each moved step runs once, in its new job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-promote-stall-and-review`. Operators have nothing to do.
