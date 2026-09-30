<!-- changelog: security -->
- **Consumer repos now get hook and settings files only from the owner-reviewed `.claude/` tree of the `stable` release, never from the `workflow-templates/.claude/` twin.** Before, a hook or settings twin whose owner-only sync PR was still pending could reach every consumer on the next sync.

The "Sync .claude/ assets from upstream" step of `.github/workflows/update_workflows.yml` now also checks out `.claude/` at the same `stable` commit. For `.claude/hooks/**`, `.claude/settings.json`, and `.claude/settings.local.json` it copies that `.claude/` file instead of the twin. If the twin and the `.claude/` copy differ, a consumer that already has the file keeps it. A guard file that exists only as a twin is not installed. Each skipped or kept file logs a `::warning::claude-guard-sync:` line. `/seed-repo` applies the same rule. Commands, scripts, and every other `.claude/` asset sync from the twin as before.

| The numbers that matter | Value |
| --- | --- |
| Paths affected | `.claude/hooks/**`, `.claude/settings.json`, `.claude/settings.local.json` |
| GitHub API calls added | 0 (one more directory in the existing sparse checkout) |
| Security finding | `unapproved-guard-twin-reaches-consumers` (#5607, A01:2021, high) |

What this means for consumer repos: nothing changes while every guard twin matches its `.claude/` copy, which is the case today. While a guard change waits for the owner's sync PR, consumers keep their current hooks and settings, and the run warns about each kept file. They pick up the change on the first sync after the owner merges it and it is released.
