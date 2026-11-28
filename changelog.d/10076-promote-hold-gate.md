<!-- changelog: changed -->
- **Promotions to `@stable` now respect a rollback hold.** After `rollback-stable.yml` rolls `@stable` back, the `promote` job of `promote-main-to-stable.yml` (a manual promotion, or the orchestrator's promotion with `target_sha`) no longer moves `stable`. It reads the hold before the fast-forward and, when the hold is active or cannot be read, logs `PROMOTE_SKIPPED reason=stable_hold_active to=<v>` or `reason=stable_hold_unknown`, raises a warning and ends successfully with `stable` and the tags untouched. Before, only the daily promote cycle and auto-release checked the hold.

| The numbers that matter | Value |
| --- | --- |
| Extra API reads per promote run | 1 |
| Kill switch | `PROMOTE_STABLE_HOLD_GATE_ENABLED=false` |

What this means for consumer repos: nothing to change. Operators: to promote a fix after a rollback, first clear the hold (commit `"active": false` to `stable-hold.json` on `ai/stable-hold`) or set `PROMOTE_STABLE_HOLD_GATE_ENABLED=false`; see "Roll back @stable" in the README.
