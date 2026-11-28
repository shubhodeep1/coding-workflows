<!-- changelog: fixed -->
- **A release gate that stopped itself for a runner wait is retried within the hour, and the merge-lease tick stops starting full CI runs while a gate runs** (Refs #10037, Refs #10266).

On 2026-11-28 a gate ran 87 minutes with every finished job green, then cancelled itself because one child run waited 27.5 minutes for a runner. The next attempt would have been the nightly schedule, about 11 hours later. Meanwhile the pipeline kept starting 45-job final CI runs. Now:

- The promote cycle records why a cancelled gate was cancelled (`PROMOTE_CYCLE_SKIPPED reason=smoke_gate_cancelled run=<id> cause=runner_wait|other|unknown`). Only a runner-wait self-cancel runs the new marker step, and the orchestrator poller then re-dispatches the cycle under the existing hourly limit and runner-wait check (`PROMOTE_CYCLE_RETRY ... reason=gate_runner_wait_cancel`). It stops once `PROMOTE_CYCLE_GATE_CANCEL_RETRY_MAX` retries (default `3`) were started in the last 24 hours, whichever main tip they ran on. A gate cancelled by a person is not retried.
- While a `test-and-mark-stable.yml` run is queued or running, the merge-lease tick defers its own new full final CI to the next tick (`RELEASE_GATE_FINAL_CI_HOLD outcome=deferred`). It reads the gate from the tick's active-run cache; a tick that had not listed active runs yet makes that one listing pass right before the dispatch. Docs-only and sync-quick final CI, reviews, runs already in progress and the review sweep's own final-CI dispatch are not held. A gate older than `RELEASE_GATE_FINAL_CI_HOLD_MAX_MINUTES` (default `300`, `0` turns it off) no longer holds merges.

What this means for consumer repos: nothing changes in consumer workflows.
