<!-- changelog: changed -->
- **The release gate's review wait stops probing a job log it cannot read.** After 3 failed fetches of `/actions/jobs/{id}/logs` in a row, the Phase 4 wait in `test-and-mark-stable.yml` skips that endpoint for 300 seconds; release run 35696603462 made 275 failed fetches in about an hour.

Status and conclusion polling continue during the pause, and the log counts as empty, as it does after a failed fetch. A successful fetch resets the count. The pause covers both the live-log shortcut fetch and the log-size fetch.
