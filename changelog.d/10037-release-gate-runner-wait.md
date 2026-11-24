<!-- changelog: fixed -->
- **The release gate no longer burns attempts on a long runner queue.** The auto-release and promote-cycle ticks now skip with `runner_wait_long` while the oldest live queued run of the repository has waited `RELEASE_GATE_MAX_RUNNER_WAIT_MINUTES` (default `30`) or longer, and the gate stops itself instead of failing when a child run timed out only because it never got a runner (Refs #10037).

Since 2026-11-21 every `Test & Mark Stable Release` attempt timed out waiting for child runs that sat 20 to 60 minutes in the runner queue, and each failure filed a heal issue that started more runs. Now:

- `scripts/auto_release_stable.sh` and `scripts/promote_main_cycle.sh` measure the runner wait (one created-range read of queued runs, wedged runs left out) and skip without spending an attempt. Once the newest gate run is `AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS` old the tick dispatches whatever the wait, so a long queue holds a release for at most one window. A failed read dispatches as before.
- A skipped promote cycle is retried by the orchestrator poller at most once an hour through a new `cycle_retry` input of `promote-main-to-stable.yml`, which only runs the guarded `cycle` job and never promotes.
- `scripts/dispatch_and_watch_workflow_run.sh` and the Phase 4 review wait classify a budget timeout as `runner_wait` when the watched run never got a runner (or got one only in the last third of the budget), and the gate then cancels itself unless a job already failed. The workflow-failure-heal intake files nothing for a gate run whose only failures are such watch steps.
- The alt-model wait applies the same check to its issue's in-flight runs at its deadline, and when the gate cancels itself its watch jobs cancel the child runs they dispatched that are still running, and its smoke and alt-model jobs cancel their test issue's unfinished pipeline runs.

What this means for consumer repos: nothing changes in consumer workflows. `RELEASE_GATE_MAX_RUNNER_WAIT_MINUTES=0` turns the new check and the promote retry off.
