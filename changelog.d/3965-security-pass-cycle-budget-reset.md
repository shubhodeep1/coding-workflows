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

- **The tracking issue body no longer advertises stale security-pass state.** Every security-pass transition that exits before tick-level reconciliation now re-renders the `### Security pass` block before posting its state comment.

On #3965 the issue body still read `Status: passed` with the previously audited SHA while the label said `ai:security-pass-failed` and the alert comment reported exhaustion, because the body was only re-rendered on the merge-conflict and wave-status paths and security-pass transitions left the tick before reaching them. The `passed`, head-changed `pending`, `blocked`, fail-closed, terminal-failure, closed-fix-failure, stall-recovery successor-adoption, and `/re-security-pass` transitions now call a shared reconcile step between the state write and the state comment, so the persisted body hash rides the comment already being posted.

| The numbers that matter | Value |
| --- | --- |
| Transitions that now re-render the body | 8 |
| Extra API calls when the body is unchanged and `project_body_snapshot` is present | 0 |
| Legacy state without `project_body_snapshot` | 1 live issue-body fetch per transition |
| API calls when the body changes | 1 `gh issue edit` |

What this means for operators: the security-pass block on a tracking issue is trustworthy at a glance. `Status`, `Completed fix cycles`, `Audited integration SHA`, and `Active fix issue` reflect the current state on each covered transition, not the last clean pass or closed predecessor issue.

### For contributors

The reconcile is `reconcile_tracking_body_after_security_pass_transition` in `scripts/orchestrate_poll_process.sh`, a thin wrapper over the existing hash-gated `reconcile_tracking_issue_body_from_state`. It reads `final_merge_pr` and `integration_branch` from state and fails open. Unlike the tick-level callers it does not skip when both are empty: the body render reads only state, and those two values feed only the readiness refresh, which guards itself, so a fail-closed transition on a project with no integration branch still re-renders. Already-stale issues such as #3965 are not self-healed on the steady-state `failed` tick, by design, to avoid a per-tick live-body fetch for legacy states with no `project_body_snapshot`; they re-render on the next transition, for example `/re-security-pass`. Regression coverage includes the terminal-failure, `blocked`, and stall-recovery successor-adoption transitions, including a second terminal-state tick that asserts no edit is issued when the body is unchanged.
