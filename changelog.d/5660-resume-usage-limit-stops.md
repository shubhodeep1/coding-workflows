<!-- changelog: added -->
- **The Claude issue pickup now resumes sessions the account usage limit stopped, on its first wake after the reset.** Checkers, stage sessions, fixers, and implementation sessions no longer wait for someone to wake them by hand.

On 2026-09-30, 47 sessions failed between 10:29Z and 10:59Z with `You've hit your session limit · resets 11am (UTC)`. 70 minutes after the reset, 46 were still stopped with nothing scheduled to wake them.

The pickup's wakes are cron Routines, which keep firing through a failed turn, so the pickup is the first session to run after a reset. On every `start`, hourly, and catch-up wake it now:
- lists sessions (3 days back) and enabled triggers;
- runs `.claude/scripts/usage_limit_resumes.py`;
- sends each selected session one `Resume after usage limit (#<N>)` trigger, spaced at 4 wakes every 3 minutes.

The script selects `IDLE` sessions whose last summary carries the usage-limit or account rate-limit error, plus checkers whose `rate_limit_info` shows a `rejected` limit that has since reset and that have no trigger at all. It skips a session that is:
- archived, running, or on a permission prompt;
- the pickup itself;
- still limited;
- bound to a wake that is already due, including an earlier resume trigger that has not fired yet.

A session whose resumed turn fails on a limit again is picked again on the next wake. Waking 57 sessions within about 10 minutes on 2026-09-30 made some resumed turns fail with `Server is temporarily limiting requests` or hit the limit again, which is why the wakes are spaced.

Nothing is resumed while the account is still limited. Checkers repeat their latest checker instructions, after checking that the stage they would start does not exist yet. Other sessions re-read their state and continue. Every prompt names `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`.

| The numbers that matter | Value |
| --- | --- |
| Sessions resumed per wake | at most 20 (`CLAUDE_USAGE_LIMIT_RESUME_LIMIT`, clamped 1..40) |
| Order | checkers first, then the oldest `updated_at` |
| Wake that blocks a resume | a pending resume trigger, or another bound trigger due within 30 minutes (for a checker, any bound trigger) |
| Wake spacing | 4 every 3 minutes, first at 2 minutes (`fire_offset_minutes`; 29 minutes at the largest cap) |
| `create_trigger` pacing | at most 8 per minute across the wake (observed limit about 9); a background 60-second wait on `Trigger creation rate limit reached` |
| Sessions listed per wake | 3 days back, at most 10 pages of 100 |
| New report fields | `limit_resumed=<n>; limit_pending=<n>` |

What this means for operators: after a usage-limit stop, stalled projects resume within one pickup wake of the reset. Check the pickup's report line for `limit_resumed` and `limit_pending`. The manual procedure in `docs/operations/master-session.md` is now only needed for sessions older than 3 days, or when the pickup itself is down. The stale Routine sweep deletes the resume triggers once they have fired.

### For contributors

The selector reads saved `list_sessions` and `list_triggers` files, including the harness's `<other-session>` envelope, and makes no API calls. Its prompts are fixed text plus a required GitHub login, so no session title or summary reaches a prompt. `tests/test_usage_limit_resumes.py` covers every skip reason, the cap, the ordering, the hold-off, the trigger names, the prompts, and malformed input, and runs in its own `ci.yml` step. `.claude/**` changes shipped twin-first: the script, `stale_routines.py`, and `settings.json` twins, with the pickup diff in the sync blocker.
