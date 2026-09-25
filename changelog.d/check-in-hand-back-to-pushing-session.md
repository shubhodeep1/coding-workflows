<!-- changelog: changed -->
- **The action-needed report for a pushed PR now comes from the session that opened it, not from the Sonnet checker.** When a PR reaches a terminal state, the CLAUDE.md §26 checker hands the verdict back to the pushing session, which writes the next steps with the context it already holds.

Until now the Sonnet checker wrote the §26 terminal report itself, from next steps the pushing session guessed at when it armed the check-in, so the "what's left" answer lived in a session that knew nothing about the work. Now the pushing session creates a poke-only Routine bound to itself before starting the checker. When `.claude/scripts/check_in_status.py` reports the PR merged or closed, the checker calls `fire_trigger` on that Routine with the verdict and renames itself `PR #<n> merged — handed to <session id>`. The pushing session wakes once, writes the report, renames itself `PR #<n> merged — <no action needed | action needed>` (or `PR #<n> closed — decision needed`), archives the checker, and sends the one push notification. `/implement-plan-claude` does the same for a blocked, closed, or stuck PR: the stage session that opened it runs the intervention instead of a fresh `… — blocked PR` session. Merged phase PRs still start a fresh stage session.

| The numbers that matter | Value |
| --- | --- |
| Wakes of the pushing session per PR | 1, at the terminal verdict |
| Check-in interval (unchanged) | 180 minutes |
| Fallback when the hand-back fails (Routine deleted, or pushing session archived) | checker writes the report itself, titled `… (pushing session unreachable)` |

What this means for operators: open the session that pushed the PR to see what is left, and it will be titled with the PR's outcome. A checker session titled `… — handed to …` needs no attention. For `/implement-plan-claude`, a blocked PR shows up in the phase session that opened it, renamed `… — blocked PR`.

### For contributors

- `fire_trigger` on a Routine bound to an archived session does not fail: it starts a fresh session with no repository or context (verified 2026-09-25). The checker therefore compares the `session_id` the call returns with the pushing session's id, archives the stray session on a mismatch, and falls back; the Routine prompt also tells a stray session to do nothing.
- The hand-back Routine is `create_trigger` with no `cron_expression`, `run_once_at`, or `persistent_session_id`. The checker's `fire_trigger` is a claude-code-remote write tool, so like `send_later` it runs without a prompt only in Auto mode.
- `.claude/hooks/pr_check_in_reminder.py` (and its `workflow-templates/` copy) now tells the session to create the hand-back Routine before the checker. `tests/test_pr_check_in_reminder.py` asserts the new reminder text and the CLAUDE.md §26 wording.
- `/implement-plan-claude` adds a **Hand-back** section, a `Hand-back trigger` field in the `— resume.` block and the progress log `Check-in:` line, and a new first branch in the checker prompt; step 0 deletes the previous stage's hand-back Routine.
