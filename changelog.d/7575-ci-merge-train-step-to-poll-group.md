<!-- changelog: changed -->
- **CI's merge-train tests now run in group 1 of the `orchestrate-poll` matrix.** The step "Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15)" moved out of `tests-hooks-and-orchestrator`, which took 15m52s of its 20-minute timeout on main (run 39038112755, issue #7575). The step's name and tests are unchanged, and an `if: strategy.job-index == 1` guard keeps it running once per CI run. No timeout was raised and no job was added.

| The numbers that matter | Value |
| --- | --- |
| `tests-hooks-and-orchestrator` on main before the move | 15m52s of 20 minutes (run 39038112755) |
| Moved step's duration | about 4.5 minutes (recorded estimate, not measured on that run) |
| `orchestrate-poll` group duration | about 5 minutes per group before the move (#4707); group 1 now adds about 4.5 minutes; 20-minute timeout |
| Group that runs the step | 1 (group 0 carries the fast-fail subset) |

What this means for operators: nothing to do. A failure in these tests now shows on `orchestrate-poll (1)`, and `CI / lint` still fails when any job fails.
