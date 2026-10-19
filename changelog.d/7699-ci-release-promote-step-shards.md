<!-- changelog: changed -->
- **CI's promote-cycle tests now run in parallel shards instead of moving to another job.** The `tests-release-and-log-analysis` job took 17m04s of its 20-minute timeout on main (run 39257527801, issue #7699) after the step "Promote cycle and blocked-verdict unit tests" came back to it in #7697. That step had already moved three times (#7677, #7689, #7697), and every other job was estimated at or above the 15-minute warning line with it added. So it stays in its job and splits the test functions of its five modules into 4 local shards, the way the merge-train step was split in #7579. The step's name, its job, its tests and the `CI / lint` check are unchanged, and every test function still runs exactly once. No timeout was raised and no job was added.

| The numbers that matter | Value |
| --- | --- |
| `tests-release-and-log-analysis` on main before the change | 17m04s of 20 minutes (run 39257527801) |
| Step duration before the change | about 6 to 7 minutes (estimate from earlier job durations, not measured on that run) |
| Processes the step now runs in parallel | 4 shards of the five modules' test functions |
| Expected job duration after the change | about 12 to 13 minutes (estimate) |

A shard that fails, or that records no exit code, fails the step. Its last 40 log lines are repeated below the collapsed log groups, each line prefixed with `| `, and the step ends with `::error::promote-cycle step shards failed: <ids>`. A collection error, or a module that collects no tests, also fails the step. The step timings of run 39257527801 could not be read when this was planned, so the next `budget-watch` run on main measures the result. Nothing to do for operators.
