<!-- changelog: added -->
- **The orchestrator poller queues one follow-up run after a tick that moved a project forward.**

GitHub runs the poller's `*/5` cron every 25 to 60 minutes, so an orchestrator project that could take its next step right away (a finished wave, an issue entering a new phase) waited for the next cron run. Now, when a tick persists a state change to a project's status, wave, integration, final-merge or security-pass status, or a wave issue's phase, the new step "Queue a follow-up poll after progress" in `.github/workflows/orchestrate_poll.yml` dispatches the poll wrapper once more through `scripts/orchestrate_force_tick.sh`. Timestamp and counter updates do not count. The new repository variable `POLL_FOLLOW_UP_ON_PROGRESS_ENABLED` is on by default; set it to `false` to turn the follow-up off.

| The numbers that matter | Value |
| --- | --- |
| Follow-up runs queued per tick | at most 1 (the poll job's concurrency group keeps one pending run) |
| Cooldown per tracking issue | 300 seconds |
| Extra GitHub API calls | the one `workflow_dispatch`, only after progress |

What this means for operators: active orchestrator projects move through consecutive steps without waiting up to an hour between them, and a quiet project costs nothing extra. Consumer repos get the step on the next `@stable` sync; it dispatches their own poll wrapper (`ai-orchestrate-poll.yml`).
