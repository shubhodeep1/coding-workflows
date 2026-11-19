<!-- changelog: changed -->
- **Queue backpressure now has hysteresis, and repository variables tune or turn it off.** The poller's hold on new dispatches while the Actions queue is backed up used to switch on and off at a single threshold that only the script's defaults set, so setting the repository variable `ACTIONS_QUEUE_BACKPRESSURE_HIGH=-1` had no effect (Refs #9421, Phase 3 of the stuck-backlog plan).

What changed:
- `orchestrate_poll.yml` now passes `ACTIONS_QUEUE_BACKPRESSURE_HIGH` (default 150), `ACTIONS_QUEUE_BACKPRESSURE_LOW` (default 50) and `QUEUED_RUN_ZOMBIE_MINUTES` (default 1440) from repository variables to the poller steps.
- The hold switches on when the queue reaches HIGH and stays on until the queue drops below LOW. Between the two it keeps the previous tick's state, which the poller saves in a small `actions/cache` entry (`queue-backpressure-state-v1-*`). If that entry is missing, the hold starts off.
- It logs `QUEUE_BACKPRESSURE outcome=on|off transition=...` when the state changes.
- Set `ACTIONS_QUEUE_BACKPRESSURE_HIGH` to `-1` (or `0`) to turn the hold off. If LOW is at least HIGH, or below 1, the hold falls back to the single HIGH threshold with one warning. A non-numeric value turns the hold off with one warning. When the queue depth is unknown, nothing is held.
