<!-- changelog: added -->
- **Pushed pull requests now get a Sonnet check-in, and `/implement-plan-claude` runs every stage in its own fresh session through to `/verify-activation` and `/deploy-activate`.** A new CLAUDE.md §26 has every interactive session start a small Sonnet checker for each PR it pushes, and the same checker drives `/implement-plan-claude` from phase to phase without ever waking the expensive session.

Until now a session pushed a branch, opened its pull request, and went quiet, and `/implement-plan-claude` waited on each phase with a Sonnet Routine that, it turns out, could not act: sessions started by a Routine with `create_new_session_on_fire` get no MCP tools and no repository, so its checker could never start the next step. Both now use a Sonnet session started with `create_session`, which does get the repository, `gh`, and the claude-code-remote tools. It runs `.claude/scripts/check_in_status.py` every 3 hours (re-armed with `send_later`), and the script, not the model, decides whether the PR merged, closed, got blocked, or is stuck. For a §26 check-in the checker writes the terminal report itself from next steps the pushing session gave it, renames itself `PR #<n> merged — …`, and sends one push notification. For `/implement-plan-claude` it starts the next stage session, titled `implement-plan <slug> — phase 2/4` (or `security-pass`, `validation`, `verify-activation`, `deploy-activate`), which archives the previous stage unless that one is waiting on you. After the completion PR merges the command runs `/verify-activation`, loops on its fix PRs, and starts `/deploy-activate` in its own session when the verdict is DORMANT. A 24-hour safety net restarts a stalled chain.

| The numbers that matter | Value |
| --- | --- |
| Check-in interval | 180 minutes |
| Checker model | `claude-sonnet-5`, in Auto mode |
| GitHub REST calls per check | 1 in terminal-only mode; non-terminal checks add 1 per 100 check runs and up to 3 for an old failure |
| "Stuck" threshold | conflict or failed check, head older than 6 hours, no workflow run active |
| Safety net | one wake of the last stage session after 1440 minutes, only if the chain stalled |
| Verify-activation cycles before asking | 3 |
| New `CLAUDE.md` section | §26 |
| Pull request | #4304 |

What this means for operators: run `/implement-plan-claude` in Auto mode (the command asks for it) and leave it; the session list shows one open session per plan, named after its current stage, and you are notified when a stage starts, when the project is LIVE, or when `/deploy-activate` is waiting for you at Step 1. After any other push, the Sonnet checker tells you when the PR lands and what is left. Neither touches CI or review comments on its own. `.claude/settings.json` now pre-approves the tools these flows call, so sessions stop asking at start-up.

### For contributors

- `.claude/scripts/check_in_status.py` (new, mirrored to `workflow-templates/.claude/scripts/`) takes `--pr N [--terminal-only]`, `--run ID`, or `--issues a,b`, reads over REST only, prints one JSON line, and exits 2 on a failed read; `tests/test_check_in_status.py` runs in its own `ci.yml` step.
- `permissions.allow` adds file edits, `claude/*` pushes, `gh` REST and run reads, the four security-audit / validate dispatches, the GitHub MCP and claude-code-remote tools, `CronCreate` / `CronDelete` / `PushNotification`, and the helper. `permissions.ask` keeps `gh api` writes behind a prompt. The claude-code-remote write tools (`send_later`, `create_session`, `archive_session`, the trigger tools) ask on every call outside Auto mode whatever the allowlist says, which is why stage and checker sessions need Auto mode and the checker runs on Sonnet.
- `.claude/hooks/pr_check_in_reminder.py` is a `PostToolUse` hook under the anchored matcher `^(?:Bash|mcp__.*__create_pull_request|mcp__.*__push_files|mcp__.*__create_or_update_file)$`; its reminder text now points at the Sonnet checker session.
