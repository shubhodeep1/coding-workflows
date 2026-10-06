<!-- changelog: changed -->
- **`CLAUDE.md` and the shipped slash commands no longer describe retired machinery.** `/deploy-activate` now runs Cloudflare steps itself under `CLAUDE.md` §24, the way it already ran DigitalOcean steps under §22.

A prompt audit found several commands that contradicted the repository. `/implement-plan-ai` called `/implement-plan-claude` an in-session implementer, but it is the Claude-engine orchestrator hand-off. `/implement-issue-claude` said the engine label stays inert until Phase 6, which has shipped. `/analyze-log` and `/investigate-issue` treated `gh auth status` as the auth check, which §23 says fails behind the Claude Code Web proxy. `/write-plan` sent implementation to `/investigate-issue` instead of the orchestrator. Commands that load project context now read the relevant sections of `README.md` and `agents.md` and no longer re-read `CLAUDE.md`, which every session already loads, and the log commands name whichever GitHub workflow-log tool the session exposes. `/audit-plans` stopped screening for `docs/implement-plan/` progress logs and `claude/implement-plan-*` PRs, which no longer exist since the session chain was retired. In `CLAUDE.md`, the §12 trigger list no longer names a PR comment, the inactive §12.G body is reduced to a stub, and the pre-task step reads the relevant sections of `README.md` and `agents.md` instead of both files in full.

| The numbers that matter | Value |
| --- | --- |
| Commands changed | `analyze-log`, `apply-analysis`, `apply-url`, `audit-plans`, `deploy-activate`, `implement-issue-claude`, `implement-plan-ai`, `investigate-issue`, `validate-consumer-issue`, `verify-activation`, `write-plan` |
| Template copies changed | `workflow-templates/.claude/commands/{apply-analysis,apply-url,audit-plans,deploy-activate,implement-plan-ai,verify-activation,write-plan}.md` |
| `CLAUDE.md` sections edited | PRE-TASK, §0, §12, §12.G, §19, §27 (numbering unchanged) |
| Cloudflare credentials | `FUNTOKEN_IO_CF` (funtoken.io), `FT_GAMES_CF` (ft.games, 5m.fun) |
| Additional template copies corrected | `validate-consumer-issue`, `implement-issue-claude` |

What this means for consumer repos: the next `@stable` sync delivers the updated `CLAUDE.md` and template commands. Section numbers are unchanged, and no rule was loosened. `/validate-consumer-issue` now searches only relevant context, and `/implement-issue-claude` makes clear that the Claude label does not switch review off OpenCode. With a Cloudflare credential present, `/deploy-activate` runs Cloudflare reads directly and runs Worker deploys after you approve each step. When Wrangler and a safe sandbox are available, a failed dry run blocks the deploy; without isolation, the command relies on GitHub check-runs. The deploy process receives only the matching Cloudflare credential in an allowlisted environment, not other session secrets that Wrangler build hooks could read. You still set Worker secret values yourself, and §24.D operations still need a Q/A approval first.

### For contributors

`tests/test_audit_plans_command.py` no longer asserts the legacy `/implement-plan-claude` screening text; it asserts that text stays out of the command.
