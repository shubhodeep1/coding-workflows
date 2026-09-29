<!-- changelog: added -->
- **A permission prompt that blocks an unattended Claude Code session is reported on GitHub as soon as it appears.** The report includes the sanitized command, so the operator no longer has to open the stuck session to see what it is waiting on.

Until now, `.claude/hooks/permission_prompt_logger.py` only logged prompts. `permission_prompts.py file` filed them at the end of a stage, which a session stuck on a prompt never reaches (issue #4707's session waited on a read-only `gh` loop for more than three hours). On every `PermissionRequest`, the hook now also starts a detached `permission_prompts.py report-now` and returns at once. In a cloud session running in `auto` or `bypassPermissions` mode, the helper posts the event, the tool, the command, the session id and link, the session title, and the pattern signature. The command is sanitized the same way as before: 2,000-character limit, heredoc bodies removed, token-like strings masked. In coding-workflows the report goes on the pattern's `ai:permission-prompt` issue, which it opens if needed. In other repositories it goes on the session's open PR, or on its `claude/implement-plan-issue-<N>-` issue. The new read-only `permission_prompts.py lookup --session <id>` returns that command and the issue link for the operator's poller.

| The numbers that matter | Value |
| --- | --- |
| Reports per signature per session | 1 (a later `file` lists it under `already_reported`) |
| Reports per session | at most 5 |
| API calls per report | 1 POST, plus the issue read `file` already makes (elsewhere, 1 open-PR read) |
| API calls per `lookup` | 1 search read plus 1 comments read per hit, at most 3 hits (where search is refused, as in Claude Code Web, 1 read per 100 `ai:permission-prompt` issues replaces it) |
| Delay added to the prompt | none (the hook does not wait for the helper) |
| Kill switch | `CLAUDE_PERMISSION_PROMPT_REPORT=off` (default: unset, on) |

What this means for operators: when a session's status reads `Waiting on permission: …`, the command is already on the `ai:permission-prompt` issue. To pause immediate reports, during a GitHub outage for example, set `CLAUDE_PERMISSION_PROMPT_REPORT=off` in the session environment; prompts are still logged and filed at the end of the stage. Run `PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/permission_prompts.py lookup --session <id>` from the master poller, or read the issue, to decide without opening the session. Wiring `lookup` into the poller's alert is an operator step, because the poller lives outside this repository.

### For contributors

The hook still prints nothing, reads no environment variables, and makes no API calls. It passes the helper the SHA-256 of the line it logged, so the helper reports that prompt even when another one is logged first. The helper decides whether the session is unattended, and any error it hits is swallowed. Its once-per-session and 5-per-session limits are kept per session id in `immediate-state.json`, and a signature is reserved there before the POST, so a local write failure never leads to a second immediate report. `/implement-plan-claude` and `/implement-issue-claude` record the session title at step 0 with `permission_prompts.py session-meta`. Sessions started any other way report the title as `not recorded`. Tests are in `tests/test_permission_prompts.py`.
