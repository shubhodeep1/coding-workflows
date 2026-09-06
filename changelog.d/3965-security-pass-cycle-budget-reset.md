<!-- changelog: fixed -->
- **A routine sync merge no longer terminalizes a project that already passed its security pass.** The bounded security-fix budget now resets when new commits invalidate a recorded clean audit, so findings in freshly-synced code get their own fix cycles instead of failing the project on sight.

The orchestrator's project security pass is SHA-bound: a clean result is valid only for the exact integration head it audited. When the head advances, the pass is invalidated and re-runs. Until now the completed-fix-cycle counter carried across that invalidation, so a project that had already spent its budget getting to a clean pass would hard-fail on the very first finding in code that arrived afterwards, without ever being granted a cycle to fix it. `run_security_pass_inline` now resets `security_pass_cycle` to `0` when the prior status was `passed` and the head has moved, logging `SECURITY_PASS_CYCLE_BUDGET_RESET ... reason=head_advanced_after_clean_pass`. The reset fires only from a `passed` prior status, so an unresolved `blocked` fix chain keeps its spent budget and still terminalizes on exhaustion.

| The numbers that matter | Value |
| --- | --- |
| Fix cycles granted to post-sync findings, before | 0 |
| Fix cycles granted to post-sync findings, after | `MAX_SECURITY_PASS_CYCLES` (default 3) |
| New GitHub API calls | 0 |
| Incident that motivated this | project #3965, passed at `75048a2c`, sync-merged to `56f71c8f`, failed 3/3 |

What this means for operators: a project that reaches a clean security pass and then receives a `chore: sync <default> into <integration>` merge, a resolver or judge conflict resolution, or a merged fix PR will now work through any new findings on its own instead of raising a CRITICAL alert and waiting for `/re-security-pass`. Completion still requires a clean SHA-bound pass at the current head, so no unaudited code reaches the default branch as a result of the fresh budget.

### For contributors

The reset lives next to the `security_pass_current_head_is_valid` early return in `scripts/orchestrate_poll_process.sh`, which is the single point both security-pass entrypoints funnel through. `ensure_security_pass_before_completion` delegates to `run_security_pass_inline`, so patching the inner call site covers both. A `jq` failure while rewriting the budget is non-fatal: the previous budget stands and the run emits a warning. Two tests cover the split — one asserts the fresh budget and the created fix issue after a head advance from `passed`, the other asserts that a `blocked` prior status still terminalizes on exhaustion.
