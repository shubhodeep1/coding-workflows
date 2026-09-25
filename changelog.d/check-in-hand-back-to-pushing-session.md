<!-- changelog: changed -->
- **The action-needed report for a pushed PR now comes from the session that opened it, not from the Sonnet checker, and the Routines these check-ins leave behind are swept.** When a PR reaches a terminal state, the CLAUDE.md §26 checker hands the verdict back to the pushing session, which writes the next steps with the context it already holds.

Until now the Sonnet checker wrote the §26 terminal report itself, from next steps the pushing session guessed at when it armed the check-in. Now the pushing session creates a hand-back Routine bound to itself, due in 7 days, before starting the checker. The checker renews it at every 3-hourly check-in; when `.claude/scripts/check_in_status.py` reports the PR merged or closed, it pulls the Routine forward to one minute out with the verdict in its prompt and confirms with `get_trigger` 10 minutes later that it ran in the pushing session. The pushing session wakes once, writes the report, renames itself `PR #<n> merged — <no action needed | action needed>` (or `PR #<n> closed — decision needed`), renames and archives the checker, and sends the one push notification. `/implement-plan-claude` does the same for a blocked, closed, or stuck PR: the stage session that opened it runs the intervention instead of a fresh `… — blocked PR` session, while merged phase PRs still start a fresh stage session. A new sweep, `.claude/scripts/stale_routines.py`, deletes fired check-in reminders, Routines whose session is gone, and hand-backs for PRs that finished more than a day ago, every time a check-in is armed or reported.

| The numbers that matter | Value |
| --- | --- |
| Wakes of the pushing session per PR | 1, at the terminal verdict |
| Hand-back horizon (renewed at each check-in) | 7 days |
| Delivery check after the hand-back | 10 minutes |
| Sweep grace for a finished PR's hand-back | 24 hours |
| Check-in interval (unchanged) | 180 minutes |

What this means for operators: open the session that pushed the PR to see what is left; it is titled with the PR's outcome, and its checker is archived under `… — handed to …`. If that session was archived first, the checker writes the report itself and adds ` (pushing session unreachable)` to its title. If a checker dies, the hand-back fires within 7 days without a verdict and the pushing session runs the check itself. The sweep only deletes Routines named `PR #<n> status check-in…`, `implement-plan <slug>: …`, or `… <owner>/<repo>#<n> hand-back`, and never one you paused. Routines left from earlier check-ins are cleared by the first sweep after this ships.

### For contributors

- The hand-back must be a scheduled fire. `fire_trigger` ignores `persistent_session_id` and starts a fresh session with no repository or context, whether the bound session is active or archived (verified 2026-09-25), so no flow calls it. A scheduled fire into an archived session ends with `auto_disabled_session_gone`, which the checker's `get_trigger` check reads as a failed hand-back.
- `.claude/scripts/stale_routines.py` (mirrored to `workflow-templates/.claude/scripts/`) reads a `list_triggers` result from a file, uses only `id`, `name`, `enabled`, and `ended_reason`, makes one REST read per distinct enabled hand-back PR, and prints `{"delete": [...], "kept", "not_ours", "errors"}`. A failed read keeps the Routine. `tests/test_stale_routines.py` runs in its own `ci.yml` step, and `.claude/settings.json` pre-approves the script and `get_trigger`.
- `.claude/hooks/pr_check_in_reminder.py` (and its `workflow-templates/` copy) now tells the session to sweep, create the bound hand-back Routine, and never use `fire_trigger`; `tests/test_pr_check_in_reminder.py` asserts the new reminder text and the CLAUDE.md §26 wording. CLAUDE.md gains §26.G for the sweep; §26.A–F keep their letters.
- `/implement-plan-claude` adds a **Hand-back** section, a `Hand-back trigger` field in the `— resume.` block and the progress log `Check-in:` line, and a hand-back branch plus a 10-minute delivery check in the checker prompt; step 0 deletes the previous stage's hand-back Routine and step 2 runs the sweep.
