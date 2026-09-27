<!-- changelog: changed -->
- **The Claude issue pickup now checks the producer runs of up to 30 queue targets per wake, three times the 10 it starts.** Queue items that fail the binding check no longer hold back the bound items queued after them.

Items that fail the binding check (`unbound`, `binding_mismatch`, `binding_untrusted`) stay open for the watchdog instead of being closed. If the pickup read the producer runs of only as many targets as it starts per wake, 10 such items at the front of the queue would take every read, and every bound item behind them would be deferred on every wake. `claude_issue_route.py queue-pending --fetch-repo` now reads the runs named by the first `QUEUE_BINDING_SCAN_FACTOR` × `--limit` targets (3 × 10 = 30) and still starts at most `--limit` of them. Items of a target past that window are deferred to the next wake, as before.

| The numbers that matter | Value |
| --- | --- |
| Targets whose producer runs are read per wake | 30 (`QUEUE_BINDING_SCAN_FACTOR` 3 × `QUEUE_PICKUP_LIMIT` 10) |
| Targets started per wake | 10, unchanged |
| Stuck items at the head of the queue that still leave all 10 start slots to bound items | 20 |
| Reads per completed producer run in the window | 1 compare read and 1 artifact download (per-run fallbacks when a listing misses) |

What this means for operators: nothing to configure. A few queue items stuck without a valid binding no longer stall the queue while they wait for someone to act on the watchdog alert. The worst case adds reads for the extra 20 targets on a wake with that many items open.
