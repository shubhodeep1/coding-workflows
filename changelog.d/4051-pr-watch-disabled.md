<!-- changelog: changed -->
- **Interactive Claude Code sessions no longer watch pull requests after pushing them.** A new CLAUDE.md §25 forbids subscribing to PR activity, offering to watch a PR, and the event-driven autofix CI / address-comments mode, and a `PreToolUse` hook enforces it.

Until now the harness prompt told a session to offer PR watching after it opened a pull request, and an accepted offer turned into a `subscribe_pr_activity` subscription that woke the session on every CI failure and review comment to autofix and reply. §25 turns that off in this repo and, through the existing root `CLAUDE.md` mirror in `update_workflows.yml`, in every consumer repo on the next `@stable` sync. The rule is strict: it holds even when the user asks for a watch in the session, and the reply names §25 and the reviewed change needed to re-enable it. Fixing CI or addressing review comments still happens when the user asks for it directly, under plain §12; the §12.G add-ons that only made sense for the event-driven mode are marked inactive and kept in place so section numbers stay stable. `.claude/hooks/pr_watch_guard.py`, wired in `.claude/settings.json` under the matcher `mcp__.*__subscribe_pr_activity`, blocks every `subscribe_pr_activity` call from any MCP server and never blocks `unsubscribe_pr_activity`; it ships to consumers through the same `.claude/` asset sync as the §21 merged-PR guard and is part of the `/seed-repo` asset set.

| The numbers that matter | Value |
| --- | --- |
| New `CLAUDE.md` section | §25 |
| Hook | `.claude/hooks/pr_watch_guard.py` |
| Settings matcher | `mcp__.*__subscribe_pr_activity` |
| Test file now run by `ci.yml` | `tests/test_pr_watch_guard.py` |
| GitHub API calls issued by the hook | 0 |

What this means for operators: after a session pushes a branch and opens its pull request, it reports the link and stops. CI failures and review comments on that PR are handled by the unattended review pipeline as before, or by a session when you ask it to in chat, never by a background subscription. Scheduled self check-ins (`send_later`, Routines) are unaffected. Nothing to configure; the hook and the §25 text arrive in consumer repos on the next sync.

### For contributors

The hook has no environment-variable escape hatch, unlike the §21 guard's `CLAUDE_PR_MERGE_GUARD`: re-enabling PR watching for a repo means editing §25 and removing the hook entry from `.claude/settings.json` in a reviewed change. The settings matcher is a regex so that a subscribe tool exposed by a future MCP server is caught without a settings edit; `tests/test_pr_watch_guard.py` asserts it selects every known subscribe tool under both anchored and unanchored regex interpretation and never selects the unsubscribe tools. The unattended pipelines never read `CLAUDE.md`, so the `review_autofix` workflow and orchestrator stall recovery are unchanged.
