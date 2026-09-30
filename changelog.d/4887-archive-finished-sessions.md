<!-- changelog: added -->
- **Finished Claude sessions are archived automatically.** Fixer sessions archive themselves when their pull request merges or closes. The Claude issue pickup's hourly wake archives the fixer, issue-start, and report sessions that nothing else archived.

Until now, three kinds of automation session were never archived: `/fix-claude-pr` fixer and hold sessions after their PR was terminal, issue-start sessions after their issue was closed, and CLAUDE.md §26.D report sessions. On 2026-09-29 the account had 66 open sessions for about 26 active projects, and the operator archived 30 by hand.
- A fixer handed a terminal PR back now does the checker bookkeeping, replies in one line, and archives itself.
- The pickup's new step 3a runs `scripts/claude_session_janitor.py` over one `list_sessions` page per wake and archives what it names. A session that is running, waiting on a permission prompt, or holding an unanswered report question is never archived.

| The numbers that matter | Value |
| --- | --- |
| Fixer / hold session archived | 2 h after its PR merged or closed (`--fixer-grace-hours`) |
| Report session archived | after 7 idle days (`--report-days`) |
| Issue-start session archived | when its issue is closed (a later stage session does not count, #5664) |
| Sessions read per wake | one `list_sessions` page of 100, cursor back 30 days (`--horizon-days`) |
| GitHub API | one REST read per distinct PR or issue, never GraphQL |

What this means for operators: the session list shows live work. Checkers, stage sessions, the pickup, `/deploy-activate`, and your own sessions with other titles are never touched, and an archived session can be unarchived.

### For contributors

The rules live in `scripts/claude_session_janitor.py` and CLAUDE.md §26.I, and `tests/test_claude_session_janitor.py` covers them. The `.claude/commands/fix-claude-pr.md` and `.claude/settings.json` changes ship through their `workflow-templates/.claude/` twins. The `claude-issue-pickup.md` edit is coding-workflows-only.
