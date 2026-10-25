<!-- changelog: changed -->
- **CI: the unblock judge, scan and orphan fix-up close tests now run in `orchestrate-poll (0)` instead of `orchestrate-poll (1)` (#8091).** Group 1 took 16m09s of its 20-minute `timeout-minutes` on `main` (run 40150026157). A job that reaches its timeout is cancelled and turns CI red with no code fault. Group 1 keeps the single-file module step (#8080):

  | Step | Old group | New group |
  |---|---|---|
  | `Unblock judge, scan and orphan fix-up close tests (replace-claude-sessions Phase 7)` | 1 | 0 |

  The run's step timings were not readable, so the step and the group were chosen from earlier recorded group totals (group 0 at 15m09s in #8080, group 3 at 15m08s in #8072). That is an estimate, not a measurement. The step's name and body are unchanged, and it still runs once per CI run. No job was added or renamed, no `timeout-minutes` changed, and `CI / lint` is unchanged. A failure in these tests now shows under `orchestrate-poll (0)`. Operators have nothing to do.
