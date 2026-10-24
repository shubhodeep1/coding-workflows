<!-- changelog: changed -->
- **CI: the three single-file orchestrate-poll test modules now run in `orchestrate-poll (1)` instead of `orchestrate-poll (0)` (#8080).** Group 0 took 15m09s of its 20-minute `timeout-minutes` on `main` (run 40122910216). A job that reaches its timeout is cancelled and turns CI red with no code fault. Group 0 keeps the fast-fail subset, and the modules moved into a step of their own in group 1, which carries only its slice and the unblock step (#8072):

  | Step | Old group | New group |
  |---|---|---|
  | `Orchestrate poll single-file module tests` (`test_orchestrate_poll_noop_suspicious_recovery.py`, `test_state_snapshot.py`, `test_run_substate_ledger.py`) | 0 (inside `Orchestrate poll process unit tests`) | 1 |

  The commands and their order are unchanged, and the modules still run once per CI run. No job was added or renamed, no `timeout-minutes` changed, and `CI / lint` is unchanged. A failure in these modules now shows under `orchestrate-poll (1)`. Operators have nothing to do.
