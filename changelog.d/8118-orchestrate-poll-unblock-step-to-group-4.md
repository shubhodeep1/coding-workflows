<!-- changelog: changed -->
- **CI: the unblock judge, scan and orphan fix-up close tests now run in `orchestrate-poll (4)` instead of `orchestrate-poll (0)` (#8118).** Group 0 took 17m01s of its 20-minute `timeout-minutes` on `main` (run 40175309052). A job that reaches its timeout is cancelled and turns CI red with no code fault. Group 0 keeps the fast-fail subset, and group 4 ran only its slice until now (#8113):

  | Step | Old group | New group |
  |---|---|---|
  | `Unblock judge, scan and orphan fix-up close tests (replace-claude-sessions Phase 7)` | 0 | 4 |

  The run's step timings were not readable, so the step and the group were chosen from recorded group totals. That is an estimate, not a measurement. The step's name and body are unchanged, and it still runs once per CI run. No job was added or renamed, no `timeout-minutes` changed, and `CI / lint` is unchanged. A failure in these tests now shows under `orchestrate-poll (4)`. Operators have nothing to do.
