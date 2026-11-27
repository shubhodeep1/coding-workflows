<!-- changelog: fixed -->
- **A release-gate dispatch that hits a GitHub rate limit now waits for the reset instead of failing the job.**

Release gate run 45927857867 failed in `orphan-workflows-test` because `gh workflow run update_workflows.yml` hit the PAT's primary REST rate limit at 03:14:38. `scripts/dispatch_and_watch_workflow_run.sh` retried five times in 32 seconds and gave up, though the limit reset at 03:23:03. One failed job fails the gate, so the night's release was lost to an 8-minute wait.

The dispatch now reads why `gh workflow run` failed. On a rate limit it waits 60 seconds for a secondary limit, or until the token's `core` reset plus 5 seconds for a primary one, then tries again. That wait does not use up one of the dispatch attempts. `DISPATCH_RATE_LIMIT_MAX_WAIT_SECS` caps the total wait of one dispatch (default `1500`, `0` turns it off). Each wait logs `DISPATCH_RATE_LIMIT_WAIT workflow=<file> kind=<primary|secondary> wait_s=<n> waited_total_s=<n> max_s=<n>`. The cap also counts the time the script's pre-dispatch run lookup spent backing off on the same rate limit, so a step stays inside its `timeout-minutes` and its timeout cancel still runs. When the next wait would pass the cap, the line ends `elapsed_s=<n> outcome=cap_reached`, the existing `failed after N attempts` error line follows and the step fails at once. Other failures keep today's backoff.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls per rate-limited attempt | 1 `GET /rate_limit` (not counted by GitHub; none for a secondary-limit message) |
| Extra API calls otherwise | 0 |
| Per-step caps in `test-and-mark-stable.yml` | validation-refresh 110 s, update_workflows 410 s, internal-memory-maintenance 230 s, orchestrate 240 s, standalone validate 180 s |
| New jobs, workflows or raised timeouts | none |

What this means for operators: a secondary limit (60 s) is now absorbed in every gate dispatch step. The caps are sized so the wait fits each step's existing `timeout-minutes`, so a primary limit whose reset is further away than a step's cap (for example the 8.5-minute wait of the incident in the 18-minute `update_workflows` step, cap 410 s) still fails that step, now at once with `outcome=cap_reached`.
