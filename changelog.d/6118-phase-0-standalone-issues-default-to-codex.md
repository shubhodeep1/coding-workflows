<!-- changelog: changed -->
- **New standalone issues go to the Codex pipeline by default again.** With `AI_ISSUE_IMPLEMENTER` unset, or set to anything other than `claude`, clarify now routes a new issue to clarify → plan → implement instead of the Claude issue queue.

This is Phase 0 (freeze) of `docs/plans/replace-claude-sessions-with-cli-engine-plan.md`, which retires the claude.ai-session-driven Claude automation. Before this change, `scripts/claude_issue_route.py` defaulted to `claude`, so every new issue without a label landed in the `ai:claude-issue-queue` that the retired sessions used to drain. Now the default is `codex`, and an invalid value also falls back to `codex`. An `ai:claude` label or `AI_ISSUE_IMPLEMENTER=claude` still routes an issue to Claude while that path exists. Orchestrator-managed issues, tracking issues and `[E2E …]` fixtures stay on Codex, as before.

| The numbers that matter | Value |
| --- | --- |
| Default of `AI_ISSUE_IMPLEMENTER` | `codex` (was `claude`) |
| Fallback for an invalid value | `codex` (was `claude`) |
| New GitHub API calls | 0 |

What this means for operators: new issues no longer wait on the Claude pickup, and repos that set `AI_ISSUE_IMPLEMENTER=codex` see no change. A repo that wants the old behaviour sets `AI_ISSUE_IMPLEMENTER=claude`, but Phase 2 of the same plan removes the Claude queue entirely.
