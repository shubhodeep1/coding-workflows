<!-- changelog: fixed -->
- **Unattended Claude sessions no longer stop at a human prompt because the Auto-mode classifier refused an optional cleanup call.** A denied `delete_trigger`, `archive_session`, or `set_session_title` is now skipped and recorded, never retried.

On 2026-09-29 the #4755 resume stage (`session_019PaJAyxWrJjeWb93aYofLY`) stopped for more than an hour. It was doing `/implement-plan-claude` resume hygiene and retried a `delete_trigger` that the classifier refused as "Interfere With Workloads". After three consecutive refusals, Claude Code asks a human, and nobody watches these sessions. The trigger it was stuck on, `trig_012QFMb1nQkFFs1JyPsnZVsE`, no longer existed. The new CLAUDE.md §26.I makes such calls housekeeping. After the first denial in a step, the rest of that step's cleanup is skipped, `cleanup skipped: <tool> denied (<reason>)` goes into the report (and the progress log), and the stage does its real work. Before deleting a trigger it knows only by id, a flow now reads it with `get_trigger` and deletes it only when it is that project's or pull request's own Routine. It states the check before the call. A not-found trigger counts as already deleted.

| The numbers that matter | Value |
| --- | --- |
| Consecutive classifier blocks before Claude Code asks a human | 3 |
| Retries of a denied cleanup call | 0 |
| Retries of an essential call (start a stage, checker, or fixer; arm a wait) | at most 1 |
| Extra API reads per targeted delete | 1 `get_trigger` (none for a trigger taken from a `list_triggers` result) |
| Flows covered | CLAUDE.md §26.C, §26.D, §26.G; `/implement-plan-claude` (resume hygiene, zombie-checker cleanup, re-arm cleanup, two-step start, hand-back, end-of-project archives, checker prompt); `/fix-claude-pr`; `/claude-issue-pickup` |

What this means for operators: a stage whose cleanup is refused now finishes its stage and reports `cleanup skipped` instead of waiting for someone to approve a prompt. Leftover Routines and sessions are still removed later by `auto_disabled_session_gone`, the stale Routine sweep (`.claude/scripts/stale_routines.py`), the next stage's zombie-checker cleanup, and the checker's stale-wait check. A `/claude-issue-pickup start — restart` that could not archive the old pickup's session reads the queue at its first hourly wake instead of at once, so the two pickups never read it at the same time.

### For contributors

The rule is defined once in CLAUDE.md §26.I. Each command states it where its cleanup happens and in its Rules. The ownership check matches `implement-plan <slug>: …` or `PR #<n> …` names and a `persistent_session_id` of that project or PR. The pickup matches its existing `Claude issue pickup: hourly` name filter instead. Tests: `tests/test_implement_plan_claude_command.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_implement_issue_claude_command.py`.
