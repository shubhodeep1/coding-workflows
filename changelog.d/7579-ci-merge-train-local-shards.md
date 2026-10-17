<!-- changelog: changed -->
- **CI's merge-train tests now run in parallel shards instead of moving to another job.** Group 1 of the `orchestrate-poll` matrix took 15m13s of its 20-minute timeout on main (run 39047759102, issue #7579) after the step "Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15)" moved there in #7575. Every other job was estimated at or above the 15-minute warning line with the step added, and the step had already moved three times. So it stays in group 1 and splits `tests/test_review_merge_train.py` by test function into 3 local shards, with the five small gh_helpers modules in a fourth process beside them, the way the workflow failure heal step was split in #7387. The step's name, its `if: strategy.job-index == 1` guard and its tests are unchanged, and every test function runs exactly once. No timeout was raised and no job was added.

| The numbers that matter | Value |
| --- | --- |
| `orchestrate-poll (1)` on main before the change | 15m13s of 20 minutes (run 39047759102) |
| Step duration before the change | about 4.5 minutes (recorded estimate, not measured on that run) |
| Processes the step now runs in parallel | 4: 3 shards of 109 merge-train test functions, 1 for the small modules |
| Expected step duration after the change | about 1.5 to 2 minutes (estimate) |

A shard that fails, or that records no exit code, fails the step. Its last 40 log lines are repeated below the collapsed log groups, each line prefixed with `| `, and the step's error line names the failed shards.

What this means for operators: nothing to do. The next `budget-watch` run on main shows the new duration of `orchestrate-poll (1)`.
