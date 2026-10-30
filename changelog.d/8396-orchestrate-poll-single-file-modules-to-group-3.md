<!-- changelog: changed -->
- **CI: the three single-file orchestrate-poll test modules now run in `orchestrate-poll (3)` instead of `orchestrate-poll (1)` (#8396).** Group 1 took 20m12s of its 20-minute `timeout-minutes` on `main` (run 40954844196). A job that reaches its timeout is cancelled and turns CI red with no code fault. This step was group 1's only pinned step besides its slice, so it moved to group 3, which has not been named by budget-watch since its unblock step left (#8072) and its slice shrank to a fifth (#8113). Group 3 also took `Validation self-test unit tests` from `static-checks` in #8394, so it now pins four steps besides its slice:

  | Step | Old group | New group |
  |---|---|---|
  | `Orchestrate poll single-file module tests` (`test_orchestrate_poll_noop_suspicious_recovery.py`, `test_state_snapshot.py`, `test_run_substate_ledger.py`) | 1 | 3 |

  The run's step timings were not readable when this was planned, so the choice of step and destination is an estimate from recorded group totals. The commands and their order are unchanged, and the modules still run once per CI run. No job was added or renamed, no `timeout-minutes` changed, and `CI / lint` is unchanged. A failure in these modules now shows under `orchestrate-poll (3)`. Operators have nothing to do.
