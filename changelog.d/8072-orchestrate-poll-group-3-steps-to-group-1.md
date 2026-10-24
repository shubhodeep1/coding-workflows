<!-- changelog: changed -->
- **CI: the unblock judge tests now run in `orchestrate-poll (1)` instead of `orchestrate-poll (3)` (#8072).** Group 3 took 15m08s of its 20-minute `timeout-minutes` on `main` (run 40112590944). A job that reaches its timeout is cancelled and turns CI red with no code fault. Group 1 is the only poll group that carries no steps moved in from other jobs since #7766, so one step moved there:

  | Step | Old group | New group |
  |---|---|---|
  | `Unblock judge, scan and orphan fix-up close tests (replace-claude-sessions Phase 7)` | 3 | 1 |

  The run's step timings could not be read, and the three extra group-3 steps could not be timed in the implement sandbox either, because PyYAML was missing there and two of them failed at collection. So only the unblock step (378 tests, the heaviest by earlier timing) moved; `Orchestrator judges on the Claude engine tests (Phase 5c)` and `Validation harness sandbox tests` stay in group 3. The step's name and body are unchanged. No job was added or renamed, no `timeout-minutes` changed, and `CI / lint` is unchanged. A failure in these tests now shows under `orchestrate-poll (1)`. Operators have nothing to do.
