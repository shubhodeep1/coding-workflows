<!-- changelog: changed -->
- **The release gate's review wait stops probing a job log it cannot read.** After 3 failed fetches of `/actions/jobs/{id}/logs` in a row, the Phase 4 wait in `test-and-mark-stable.yml` skips that endpoint for 300 seconds; release run 35696603462 made 275 failed fetches in about an hour.

Status and conclusion polling continue during the pause, and the log counts as empty, as it does after a failed fetch. A successful fetch resets the count. The pause covers all three job-log fetches of the wait: the live-log shortcut fetch, the log-size fetch and the bait-coverage check, which reports the logs as unavailable during the pause and checks again after it.

What this means for release operators: a review wait whose job logs cannot be read keeps polling the run's status, but sends far fewer failing log requests.
