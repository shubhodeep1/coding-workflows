<!-- changelog: added -->
- **The hourly Claude issue pickup now archives finished automation sessions and reports sessions stuck on a permission prompt.** The session list stops growing, and a stalled session reaches a human within about an hour instead of whenever someone looks.

Until now nothing archived a `/fix-claude-pr` fixer, a CLAUDE.md §26 checker, an issue implementation session, or an issue-mode `implement-plan` stage or checker once its pull request or issue was finished; the supervising session archived them by hand. On every `— wake.`, the pickup (`.claude/commands/claude-issue-pickup.md`, step 3a) now lists the sessions and Routines, runs `.claude/scripts/stale_sessions.py`, and archives each session it names. A session is named only when its title is one the automation sets, it is not running or waiting on a permission prompt, no enabled Routine is bound to it, and its pull request or issue was merged or closed at least 24 hours ago, or it is a fixer that a newer fixer for the same pull request replaced. Operator sessions, the pickup itself, and `/deploy-activate` sessions are never archived. The same run lists every session, whatever its title, that has waited on a permission prompt for more than 20 minutes: the pickup sends one push notification per stall and files it as an `ai:permission-prompt` issue with the session's title and task summary. `/fix-claude-pr` now runs the permission prompt report before every report, as `/implement-plan-claude` stages already do.

| The numbers that matter | Value |
| --- | --- |
| Grace after a PR or issue is merged or closed | 24 hours (`--grace-hours`) |
| Prompt stall threshold | 20 minutes (`--prompt-stall-minutes`) |
| Sessions read per wake | up to 10 pages of 100, back to 14 days |
| GitHub API calls | 1 REST read per distinct PR or issue checked |
| Notifications per stall | 1 |

What this means for operators: finished sessions disappear from the session list a day after their work merges, and an unattended session stuck on a prompt shows up as a push notification and an `ai:permission-prompt` issue. Archived sessions can be unarchived. The pickup's one-line report ends with `archived <a>; stalls <s>`.

### For contributors

`stale_sessions.py` never archives anything itself; it prints one JSON line (`archive`, `kept`, `not_ours`, `already_archived`, `errors`, `stalled_on_prompt`, `stall_log_dir`, `next_after_id`) and exits 2 on unreadable input. Stall state lives in `~/.claude/stalled-sessions/` in the pickup's session, where each new stall is written as a `PermissionRequest` record with tool `StalledSession(<tool>)` for `permission_prompts.py file --log-dir`. Tests: `tests/test_stale_sessions.py`, run in its own `ci.yml` step.
