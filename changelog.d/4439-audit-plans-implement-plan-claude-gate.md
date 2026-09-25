<!-- changelog: changed -->
- **`/audit-plans` now screens its recommendation against running `/implement-plan-claude` projects, not just AI-orchestrator projects.** It no longer recommends a plan that overlaps one of those projects or that one is already implementing, and it never archives a plan such a project still owns.

Until now the step-6 merge-conflict gate only looked at open `ai:orchestrator-tracking` issues and their `orchestrator/project-*` PRs. A project driven by `/implement-plan-claude` has no tracking issue, so the audit could not see it. Step 6 now also finds those projects from two sources: progress logs `docs/implement-plan/<slug>.md` with `Status: IN_PROGRESS` or `BLOCKED`, and open PRs whose head or base branch starts with `claude/implement-plan-`. It counts each project's open-PR files and the plan files of its unticked phases, or every file the plan names once all phases have merged. A plan either runner is already implementing is reported as in flight instead of being recommended. Step 7 skips archiving such a plan, because the project's own completion PR moves it to `docs/completed/`.

| The numbers that matter | Value |
| --- | --- |
| Project runners screened | 2 (AI orchestrator, `/implement-plan-claude`) |
| Extra GitHub calls per run | 0 new listings; the existing open-issue and open-PR listings serve both |
| `/implement-plan-claude` sources | `docs/implement-plan/*.md` logs (`IN_PROGRESS` / `BLOCKED`) and open `claude/implement-plan-*` PRs |

What this means for operators: a `/audit-plans` pick is now safe to start while `/implement-plan-claude` projects are running, and a running project's plan stays in `docs/plans/` until its completion PR moves it. When GitHub cannot be read, the audit still screens against the local progress logs but marks the pick UNSCREENED. Both command copies (`.claude/commands/` and `workflow-templates/.claude/commands/`) carry the change.

### For contributors

`tests/test_audit_plans_command.py` pins the command text: that the two copies are identical, the two in-flight sources, batched GitHub reads, set-aside of in-flight plans, the archive skip, and screening from the logs when GitHub is unreadable. It runs in its own `ci.yml` step.
