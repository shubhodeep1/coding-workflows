<!-- changelog: added -->
- **The hourly Claude issue pickup now reports sessions stuck on a permission prompt.** A stalled unattended session reaches a human within about an hour instead of whenever someone looks.

On every `— wake.`, after the CLAUDE.md §26.I stale session sweep (step 3a), the pickup (`.claude/commands/claude-issue-pickup.md`, step 3b) runs `.claude/scripts/stale_sessions.py --stalls-only` on the newest `list_sessions` page. It lists every session, whatever its title, that has waited on a permission prompt for more than 20 minutes; the pickup sends one push notification per stall and files it as an `ai:permission-prompt` issue with the session's title and task summary. This step archives nothing: the §26.I sweep stays the only archiver. `/fix-claude-pr` now runs the permission prompt report before every report, as `/implement-plan-claude` stages already do.

| The numbers that matter | Value |
| --- | --- |
| Prompt stall threshold | 20 minutes (`--prompt-stall-minutes`) |
| Sessions read per wake for stalls | the newest page of 100 |
| GitHub API calls by the stall check | 0 (filing: 1 POST per new pattern) |
| Notifications per stall | 1 |

What this means for operators: an unattended session stuck on a prompt shows up as a push notification and an `ai:permission-prompt` issue. The pickup's one-line report gains `; stalls <s>`.

### For contributors

In `--stalls-only` mode `stale_sessions.py` prints one JSON line with an empty `archive` list, `stalled_on_prompt`, `stall_log_dir`, and a null `next_after_id`, and exits 2 on unreadable input. Without the flag it still carries the plan's D7 archive rules (24-hour grace, superseded fixers), which nothing runs since the §26.I janitor took that job. Stall state lives in `~/.claude/stalled-sessions/` in the pickup's session, where each new stall is written as a `PermissionRequest` record with tool `StalledSession(<tool>)` for `permission_prompts.py file --log-dir`. The pickup runs that filing on every wake: its `filed-state.json` advances only after a successful POST, so a failed filing is retried on the next wake without a second notification. Tests: `tests/test_stale_sessions.py`, run in its own `ci.yml` step.
