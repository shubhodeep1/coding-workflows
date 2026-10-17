<!-- changelog: changed -->
- **CI's merge-train tests now run in group 1 of the `orchestrate-poll` matrix.** The step "Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15)" moved out of `tests-hooks-and-orchestrator`, which took 15m52s of its 20-minute timeout on main (run 39038112755, issue #7575). The step's name and tests are unchanged, and an `if: strategy.job-index == 1` guard keeps it running once per CI run. No timeout was raised and no job was added.

What this means for operators: nothing to do. A failure in these tests now shows on `orchestrate-poll (1)`, and `CI / lint` still fails when any job fails.
