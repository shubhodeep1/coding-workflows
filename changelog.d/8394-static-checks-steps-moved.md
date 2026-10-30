<!-- changelog: changed -->
- **The three test steps moved out of the CI job `static-checks`.** That job took 11m17s of its 15-minute timeout on `main` (run 40954844196, budget-watch issue #8394). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Implement post-Codex recovery unit tests` | `tests-hooks-and-orchestrator` |
  | `Validation self-test unit tests` | `orchestrate-poll`, group 3 only (`if: strategy.job-index == 3`) |
  | `Validation bootstrap and family direct-run tests` | `tests-promote-stall-and-review` |

  The pick is an estimate, not a measurement: the run's step timings could not be read from the implement sandbox, which has no network. These three were the only test steps in `static-checks` (moved there in #7250 and #7334); the job measured about 2 minutes before those moves, and it again runs only its static checks plus the small contract tests for its own guards. The steps are spread over three jobs so that no single destination takes all of them. The self-test step, the largest by test and subprocess count, goes to `orchestrate-poll` group 3, whose only other pinned steps are two single-module steps and whose slice shrank from 1/4 to 1/5 in #8113. The bootstrap step goes to `tests-promote-stall-and-review`, the job with the most recorded room (#8278). The small post-Codex step goes to `tests-hooks-and-orchestrator`, which shed several steps in #8281. None of the steps needs git history or Node. If a destination job comes near its timeout, the next `budget-watch` run on `main` opens an issue for it that links the run, whose step timings show what to move.

  `tests/test_ci_poll_test_sharding.py` now checks that each moved step runs once, in its new job, after that job installs its Python dependencies, that it still names all its test modules, and that the `orchestrate-poll` step is pinned to group 3. No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the new job's check-run (`orchestrate-poll (3)` for the self-test step; the other groups show it as skipped). Operators have nothing to do.
