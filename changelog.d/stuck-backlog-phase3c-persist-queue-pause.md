<!-- changelog: changed -->
- **The persistence budget no longer escalates issues for time spent in a jammed Actions queue, or for rounds that failed only on base-branch failures.** `scripts/persist_budget.py decide` counted queue waits toward `PERSIST_MAX_HOURS` and inherited-only failed rounds toward `PERSIST_MAX_ROUNDS`, so either could label an issue `ai:needs-human` for problems it did not cause (Refs #9421, Phase 3 of the stuck-backlog plan).

What changed:
- The poller now records queue-pause intervals in the existing queue backpressure marker (`QUEUE_BACKPRESSURE_STATE_FILE`, `{"active", "intervals"}`), which the existing "Restore/Save queue backpressure state" cache steps in `orchestrate_poll.yml` already carry between ticks; no workflow step is added. New subcommand `persist_budget.py record-pause` opens an interval when backpressure turns on (queue depth at or above `ACTIONS_QUEUE_BACKPRESSURE_HIGH`) and closes it when backpressure turns off (depth below `ACTIONS_QUEUE_BACKPRESSURE_LOW`), keeping at most 200 intervals and none older than `2 * PERSIST_MAX_HOURS`.
- While the queue is backed up the governor skips the issue (`PERSIST governor outcome=skipped reason=queue_backpressure_pause issue=<n>`) without any API call. Afterwards, `decide --paused-intervals-file` subtracts the paused time from the `max_hours` window.
- Rounds whose gate shows no PR-caused failures and at least one inherited failure no longer count toward `max_rounds`. The re-plan triggers for repeated failures and no progress are unchanged. The decision JSON gains `rounds_exempt_inherited`.
- Two new repository variables, both default `true`: `PERSIST_QUEUE_PAUSE_ENABLED` and `PERSIST_INHERITED_ROUNDS_EXEMPT`. Set either to `false` to restore the earlier behaviour.

What this means for consumer repos: nothing changes unless the persistence budget (`PERSIST_BUDGET_ENABLED`, default off) is on; the marker is recorded either way, so the pause history is there when it is switched on.
