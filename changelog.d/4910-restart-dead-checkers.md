<!-- changelog: added -->
- **The hourly Claude issue pickup now restarts dead `/implement-plan-claude` checkers.** It also re-queues an issue whose logged checker no longer exists.

A project checker keeps its chain alive by re-arming itself every hour. When a re-arm failed, the project stalled until the stage's 24-hour safety net fired, or indefinitely when none was pending. The operator's restart rule (Q63) lived only in an ad hoc poller session's prompt. The pickup's new step 3b applies it on every wake. `scripts/claude_checker_restart.py` decides and the pickup applies the result: a one-shot `implement-plan <slug>: check-in` trigger into the checker, plus an `ai-checker-restart:<YYYYMMDDTHHMMZ>` session tag as the 3-hour marker. When a progress log names a checker that no longer exists, the pickup comments `/reclarify` on the issue, at most once per 24 hours, and the resumed session arms a new checker.

| The numbers that matter | Value |
| --- | --- |
| Quiet window before a restart | 90 minutes with no project session, the checker included, created or updated, and none running or waiting |
| Restart cooldown per checker | 3 hours (session tag) |
| Re-queue cooldown per issue | 24 hours (any trusted `/reclarify`) |
| `get_session` lookups per wake | at most 8, rotated hourly |
| REST reads per wake | 1 open-issue list, plus 1 issue or comment read per candidate |

What this means for operators: a checker that dies no longer needs the Master poller or a person to notice. Once this merges and the pickup has picked it up, the supervising session can remove the dead-checker rule from the poller's prompt.

### For contributors

The page read is the newest `list_sessions` page, because condition 4 needs every session created in the last 90 minutes. Older checkers are found from pending `implement-plan <slug>: safety net` prompts and from progress logs, which are read over git (`git ls-remote` plus one shallow `git fetch`). A trigger page with `has_more` restarts nothing and re-queues nothing. A checker that itself ran in the last 90 minutes is kept, because it may have re-armed or started its next stage after the trigger and session pages were read. For the same reason a checker is restarted only from a fresh `get_session` record: a page-listed checker whose page record would restart it is looked up too (within the 8-lookup cap), and one known only from the page is kept until a later wake looks it up. Tests live in `tests/test_claude_checker_restart.py`, with their own `ci.yml` step.
