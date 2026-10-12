<!-- changelog: changed -->
- **Telegram now pages only when a person has to act.** Alerts for work the pipeline is already handling are sent at `DEBUG`, and the default `ALERT_MSG_LEVEL` threshold is now `WARNING`, so they stay out of the chat.

Before this change the chat got a `WARNING` or `CRITICAL` for every stall-recovery step, every conflict-resolver dispatch, every re-issue, and even for successes such as "Review-blocked judge merged PR as-is". Messages like "Standalone PR #6844 has merge conflicts. Review workflow dispatched for resolution." and "Activation DORMANT … (1 code gap(s), 0 operator step(s))" described work the pipeline was already doing. The level convention is now explicit in `scripts/tg_helpers.sh`: `DEBUG` means the pipeline owns the event, `WARNING` and above mean a human is needed. Final integration merges and project-complete messages keep paging on every completion path. Set `vars.ALERT_MSG_LEVEL=DEBUG` to get the full stream back.

| The numbers that matter | Value |
| --- | --- |
| Default `ALERT_MSG_LEVEL` | `WARNING` (was `DEBUG`) |
| Poller alerts moved to `DEBUG` (`scripts/orchestrate_poll_process.sh`) | 91 call sites |
| Standalone conflict attempts before one `WARNING` | 3 (`STANDALONE_CONFLICT_STUCK_ALERT_ATTEMPTS`, `0` disables) |
| Project-judge output failures before the `CRITICAL` | 3 (`JUDGE_OUTPUT_FAILURE_MAX`, unchanged) |

Repeating human alerts now page once. The no-op force-merge gate failures page once per PR head and gate. The comprehensive release-callback dispatch failure pages once per failure streak. The `MAX_MERGE_DEFERRALS` hand-over sends one notice on the tick the cap is reached. The project judge's "Manual review needed" `CRITICAL` fires only when the judge reaches `JUDGE_OUTPUT_FAILURE_MAX` and fails the project; earlier failures are retried silently. The review-blocked judge's merge and re-issue alerts now follow what actually happened: a merge that went through, waits for a branch update or retries on its own is `DEBUG`, while a failed merge, failing required checks, `ENABLE_AUTO_MERGE=false` or a closed PR without its replacement issue still pages. Activation verdicts page only when the verdict lists operator steps, or when the code-gap fix issue could not be checked or opened.

The standalone PR conflict sweep counts its automatic resolution attempts per PR head in one marker comment on the PR (`<!-- ai:standalone-conflict-attempts:v1 … -->`). When the same head is still conflicted after 3 attempts it sends one `WARNING`, which the sweep never did before.

Two invalid levels are fixed. `"WARN"` in `review_autofix.yml` and `"INFO"` in the poller were filtered as `DEBUG` but shown with the 🚨 icon. Several messages that said "manual review needed" on paths the pipeline retries now say what happens next.

What this means for operators: every Telegram message at the default threshold is something to act on, apart from final-merge and project-complete notices. Consumer repos pick up the new default on their next `@stable` sync. Any repo that already sets `vars.ALERT_MSG_LEVEL` keeps its own threshold.

### For contributors

Pick the level by who acts next. Use `DEBUG` when the pipeline dispatches, retries, re-issues or recovers, or for a success ping. Use `WARNING` or higher when a person must act. When an auto-handled path can loop forever, add a once-per-condition alert for the stuck case rather than paging on every attempt. `tests/test_alert_levels_human_only.py` pins the default threshold, rejects invalid level strings, and covers the new once-only helpers.
