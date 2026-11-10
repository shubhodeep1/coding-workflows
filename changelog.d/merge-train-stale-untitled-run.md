<!-- changelog: fixed -->
- **The merge train no longer stops releasing PRs when GitHub leaves a review run stuck before it starts.** A daily Telegram warning now reports a queue that has not moved for a day.

From 2026-11-04 the train in `shubhodeep1/coding-workflows` released nothing for six days and 16 pull requests stayed labelled `ai:merge-queued`. GitHub had accepted an `Internal: AI Review & Autofix` dispatch (run 41814678900) and never started it: no jobs, no PR number in its title. `scripts/review_merge_train.sh release` treats an untitled review dispatch as one that could belong to any queued PR, so every pass ended `MERGE_TRAIN_RUNS_LISTING outcome=incomplete reason=unattributed_run` and left every PR queued. GitHub refused to cancel the run, so nothing an operator could do would clear it. The release pass now drops an untitled default-branch review dispatch that has not started and is older than 6 hours, and logs it. The same pass, on orchestrator poll ticks, sends one Telegram `WARNING` a day while the oldest queued PR has been open for 24 hours or more.

| The numbers that matter | Value |
| --- | --- |
| Age after which a never-started untitled review dispatch is ignored (`MERGE_TRAIN_STALE_UNATTRIBUTED_RUN_HOURS`) | 6 hours, `0` turns the rule off |
| Statuses the rule applies to | `queued`, `requested`, `pending`, `waiting` |
| Queue age that triggers the warning (`MERGE_TRAIN_STALL_ALERT_HOURS`) | 24 hours, `0` turns it off |
| When the warning may be sent (`MERGE_TRAIN_STALL_ALERT_HOUR_UTC`) | a 3-hour slot from 01:00 UTC, once per day |
| Extra GitHub API calls outside that slot | 0 |

What this means for operators: queued PRs in coding-workflows are released on the first poll tick that runs this version, and each release dispatches one review. A queue older than a day now reaches Telegram with the oldest PR and that pass's `released`, `overlap`, `active_review` and `runs_incomplete` counts. The warning reports the age, not a cause, so it also fires when a PR is waiting behind an older PR that needs a person.

### For contributors

All three settings are read from the script's environment and have no repository variable; the defaults apply. The warning's once-a-day state is a comment on the oldest queued PR whose first line is `<!-- merge-train:stalled alert_date=YYYY-MM-DD -->`, where `alert_date` is the UTC date the 3-hour slot started, so a slot that crosses midnight still sends once. `orchestrate_poll.yml` passes the Telegram settings and `MERGE_TRAIN_TG_HELPERS` to the release step; `cancel_on_pr_close.yml` does not, so a PR close event never sends it. An `in_progress` untitled run, a younger one, or one with no readable `created_at` still makes the listing incomplete.
