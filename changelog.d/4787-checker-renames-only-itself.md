<!-- changelog: fixed -->
- **§26 checkers now rename only their own session, and a hand-back whose Routine was swept no longer starts a duplicate fixer.** Fixers check the checker's title with `get_session` before they rename or archive it, and every archive is reported only after `archive_session` succeeded.

On 2026-09-28 the PR #4706 checker set its terminal title on its own session and on the operator's master session, a `notify` subscriber, and reported an archive that never happened. The same day the PR #4704 checker found its fixer's hand-back Routine missing (the §26.G stale Routine sweep had deleted it after it fired successfully), read that as a gone fixer, and started a second fixer that sat on a permission prompt for about 50 minutes. CLAUDE.md §26.C step 5 now has the checker take its own id from `CLAUDE_CODE_REMOTE_SESSION_ID`, never pass a subscriber's id to `set_session_title` or `archive_session`, and call `get_session` on the subscriber before treating a missing Routine as gone. Before it starts a fresh fixer it re-runs `check_in_status.py --hand-back` and starts one only while `action` is still `hand_back_fixer`. §26.D has the fixer check the checker's title (`PR #<n> status check-in`, or the terminal `… — handed to …` title), archived state, and id before it renames or archives it. `/implement-plan-claude` applies the same check before it archives its project checker, and its checker's step 4b uses the same not-found rule.

| The numbers that matter | Value |
| --- | --- |
| Files carrying the rules | `CLAUDE.md` §26.B–§26.D, `.claude/commands/implement-plan-claude.md`, `.claude/commands/fix-claude-pr.md`, `.claude/commands/claude-issue-pickup.md` (with their `workflow-templates/` twins) |
| Extra reads before a fresh fixer | one `check_in_status.py --hand-back` run (REST only) |
| Extra reads before archiving a checker | one `get_session` call |
| Test | `tests/test_check_in_session_targeting.py`, in the `ci.yml` step "Claude-fixer hand-back, claim and catch-all sweep tests" |

What this means for operators: a checker can no longer retitle your own session, and "checker archived" in a report now means the call succeeded. A skipped rename or archive is reported in one line with the reason (title, archived, this session, or not found). Checkers left idle by the two incidents are not cleaned up retroactively; they hold no pending trigger.

### For contributors

`.claude/scripts/stale_routines.py` is unchanged: it still deletes fired `PR #<n> hand-back` Routines, and the checker no longer treats their absence as failure. Consumer repos receive the rules through the next `@stable` sync of `CLAUDE.md` and the `.claude/commands` twins.
