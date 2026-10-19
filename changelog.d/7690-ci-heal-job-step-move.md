<!-- changelog: changed -->
- **Two test steps moved out of the CI job `tests-heal-plan-and-validation`.** That job took 15m26s of its 20-minute timeout on `main` (run 39230958248, budget-watch issue #7690). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names; each now runs in one group of the `orchestrate-poll` matrix:

  | Step | New job |
  |---|---|
  | `Regression tests wired by the CI wiring guard (part 2)` | `orchestrate-poll`, group 2 only |
  | `Unblock judge, scan and orphan fix-up close tests (replace-claude-sessions Phase 7)` | `orchestrate-poll`, group 3 only |

  The run's step timings could not be read, so each step of the source job was timed in the implement sandbox and the two heaviest were moved. The numbers are relative weights, not CI times: PyYAML was missing in the sandbox, so some modules errored and the sharded heal step stopped early. Part 2 took 37.2s and the unblock step 28.2s, the two largest of the steps that ran (part 1 25.2s, clarify loop guard 20.0s, the rest 3s or less). They went to the poll matrix because every single test job has reached about 15 minutes or more in recent weeks (#7366, #7406, #7560, #7575, #7576, #7677), while poll groups record about 5 minutes; groups 0 and 1 already carry extra steps. Each moved step has an `if: strategy.job-index == N` guard, so it runs once per CI run, and `tests/test_ci_poll_test_sharding.py` now fails if a step in that job loses its group guard. The next `budget-watch` run on `main` checks the real durations.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under `CI / orchestrate-poll (2)` or `(3)`. Operators have nothing to do.
