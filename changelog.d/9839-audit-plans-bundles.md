<!-- changelog: added -->
- **`/audit-plans` can now recommend a bundle of up to 3 related plans to run as one orchestrator project.** `/implement-plan-ai` and `/implement-plan-claude` accept that bundle and dispatch it once.

Plans that touch the same code could not run as separate orchestrator projects at the same time, because each would conflict with the other, so the conflict gate let only one of them through per audit. Step 6 of `.claude/commands/audit-plans.md` now starts from the conflict-screened pick and adds other conflict-free plans that share files or a tightly-coupled unit with it, or depend on it. The report's final block lists the bundle in dependency order, cites how each member is related, and prints one `Hand off:` command. `.claude/commands/implement-plan-ai.md` gained a `## Bundles` section: one dispatch and one tracking issue for the whole bundle, one reference per plan, each plan's own phase split kept, and cross-plan DAG dependencies so no two open PRs change the same file.

| The numbers that matter | Value |
| --- | --- |
| Plans per bundle | 2 to 3 |
| Phases per bundle, summed across plans | at most 8 |
| Orchestrator dispatches per bundle | 1 |

What this means for operators: when `/audit-plans` ends with a bundle, paste its `Hand off:` line into `/implement-plan-claude` (or `/implement-plan-ai`) to queue the related plans as one project instead of running them over several audit cycles. Unrelated plans are never bundled, a bundle is never offered when the conflict gate could not read GitHub state, and single-plan recommendations and hand-offs work as before.

### For contributors

`tests/test_audit_plans_command.py` pins the bundle rules, the output block, the single-dispatch rule, and live/template parity for `implement-plan-ai.md` and `implement-plan-claude.md`. Consumer repos receive the updated commands from `workflow-templates/.claude/commands/` on the next `@stable` sync.
