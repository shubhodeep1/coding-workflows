<!-- changelog: added -->
- **Unattended Claude sessions repair a broken environment before giving up, and the queue watchdog re-queues the ones that still stop.** A session that starts without `gh`, without its deferred GitHub tools loaded, or without a checkout no longer ends on a question nobody reads.

`/implement-issue-claude`, `/implement-plan-claude`, and `/fix-claude-pr` now load the deferred `mcp__github__*` and claude-code-remote tools with ToolSearch before calling them missing. They re-run `.claude/hooks/session-start.sh` once when `gh` is absent, and attach and clone a missing checkout. When that still fails, the issue gets `<!-- ai:claude-blocked:v1 reason=environment-tools-missing -->`, `environment-checkout-missing`, or `environment-remote-tools-missing`, never an in-session question. A new hourly step in `claude-issue-queue-watchdog.yml` re-queues those issues through the same `claude-issue` dispatch `/reclarify` sends, so the Claude issue pickup starts a fresh session on a fresh container. The same step closes queue items whose issue was closed after it was queued, and the pickup refuses them.

| The numbers that matter | Value |
| --- | --- |
| Re-queues per issue before the alert | 2 per rolling 24 hours (`CLAUDE_ISSUE_ENV_REQUEUE_MAX`, `CLAUDE_ISSUE_ENV_REQUEUE_WINDOW_HOURS`) |
| Time from blocker to fresh session | within about 2 hours (hourly watchdog at `:17`, hourly pickup) |
| Silent-death retry | label still present `CLAUDE_ISSUE_QUEUE_STALE_HOURS` (default 3) after a re-queue |
| Hook failure line | `[session-start] gh_install=failed reason=<reason> exit_code=<n> at=<UTC>` |
| Hook marker file | `~/.claude-session-start-gh-install` (`SESSION_START_GH_MARKER_FILE`) |

What this means for operators: an issue that stopped on an environment failure recovers without a master session. You hear about it once, as a Telegram ERROR, only after two automatic retries in a day failed; the watchdog then leaves that issue alone until you fix the environment and comment `/reclarify`, which restarts the count. A plain `<!-- ai:claude-blocked:v1 -->` blocker is still a decision for you and is never re-queued.

### For contributors

The hook's `install_gh` previously ran under `install_gh || log …`, where bash ignores `set -e`. A failed `apt-get install` therefore fell through to `gh installed:` and returned success. Every step is now checked, with a reason. The decisions live in `scripts/claude_issue_route.py` (`env_requeue_decision`, `env_requeue_plan`, `drop_closed_targets`, `queue_closed_targets`); the writes live in `scripts/claude_issue_queue_watchdog.sh` (`CLAUDE_ISSUE_WATCHDOG_MODE=env-requeue`). Only markers from trusted authors count, and the intake re-authorizes every dispatch. The `.claude/**` command and hook changes land through their `workflow-templates/.claude/**` twins.
