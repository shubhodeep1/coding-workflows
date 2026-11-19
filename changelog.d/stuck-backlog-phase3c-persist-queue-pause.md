<!-- changelog: changed -->
- **The persistence budget no longer escalates issues for time spent in a jammed Actions queue, or for rounds that failed only on base-branch failures.** `scripts/persist_budget.py decide` counted queue waits toward `PERSIST_MAX_HOURS` and inherited-only failed rounds toward `PERSIST_MAX_ROUNDS`, so either could label an issue `ai:needs-human` for problems it did not cause (Refs #9421, Phase 3 of the stuck-backlog plan).

What changed:
- The poller now keeps a queue backpressure marker (`{"active", "intervals"}`) under `RUNNER_TEMP`, carried between ticks through `actions/cache` in `orchestrate_poll.yml`. New subcommand `persist_budget.py record-pause` opens an interval when the queue goes over `ACTIONS_QUEUE_BACKPRESSURE_HIGH` and closes it when the queue drains, keeping at most 200 intervals and none older than `2 * PERSIST_MAX_HOURS`.
- While the queue is backed up the governor skips the issue (`PERSIST governor outcome=skipped reason=queue_backpressure_pause issue=<n>`) without any API call. Afterwards, `decide --paused-intervals-file` subtracts the paused time from the `max_hours` window.
- Rounds whose gate shows no PR-caused failures and at least one inherited failure no longer count toward `max_rounds`. The re-plan triggers for repeated failures and no progress are unchanged. The decision JSON gains `rounds_exempt_inherited`.
- Two new repository variables, both default `true`: `PERSIST_QUEUE_PAUSE_ENABLED` and `PERSIST_INHERITED_ROUNDS_EXEMPT`. Set either to `false` to restore the earlier behaviour.

What this means for consumer repos: nothing changes unless the persistence budget (`PERSIST_BUDGET_ENABLED`, default off) is on; the marker is recorded either way, so the pause history is there when it is switched on.
