<!-- changelog: changed -->
- **CI: the two steps `orchestrate-poll (3)` gained in #8394 and #8396 now run in `static-checks` and `tests-promote-stall-and-review` (#8472).** Group 3 took 19m30s of its 20-minute `timeout-minutes` on `main` (run 41071474282). A job that reaches its timeout is cancelled and turns CI red with no code fault. Group 3 had stayed under budget-watch's warning line with its slice and its two single-module steps until both of these steps arrived together, so both moved out:

  | Step | Old location | New job |
  |---|---|---|
  | `Orchestrate poll single-file module tests` (`test_orchestrate_poll_noop_suspicious_recovery.py`, `test_state_snapshot.py`, `test_run_substate_ledger.py`) | `orchestrate-poll`, group 3 | `static-checks` |
  | `Validation self-test unit tests` | `orchestrate-poll`, group 3 | `tests-promote-stall-and-review` |

  The run's step timings were not readable when this was planned, so the choice of destinations is an estimate from recorded job totals. Step names and commands are unchanged; only the `strategy.job-index == 3` guard was dropped, because the new jobs have no matrix and the guard would skip the step. Group 3 now pins only the Claude engine judges and validation harness sandbox steps. No job was added or renamed, no `timeout-minutes` changed, and `CI / lint` is unchanged. A failure in these tests now shows under `static-checks` or `tests-promote-stall-and-review` instead of `orchestrate-poll (3)`. Operators have nothing to do.
