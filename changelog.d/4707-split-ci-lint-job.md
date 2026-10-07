<!-- changelog: changed -->
- **`CI` now runs as parallel jobs instead of one 40–45 minute `lint` job.** The aggregate status is still called `CI / lint`, and it fails when any job fails.

`.github/workflows/ci.yml` used to run about 120 steps one after another in a single `lint` job. On 2026-09-28 that job was cancelled on `main` at its 45-minute limit while every test was still passing. The steps now run in parallel jobs: `static-checks`, four test jobs that each take a slice of the old step order, and a four-group `orchestrate-poll` matrix. The final `lint` job needs all of them and runs with `if: always()`. It fails unless every job succeeded, so a failure or cancellation can never read as a skipped, passing check. No test was dropped, and no step body changed except the orchestrate-poll split. The release gates' `validate-scripts` jobs in `mark-stable.yml` and `test-and-mark-stable.yml` keep one runner, and their budget goes from 45 to 60 minutes.

| The numbers that matter | Value |
| --- | --- |
| Old `lint` job | 1 job, 40–45 minutes; `timeout-minutes: 45`, raised to 60 by the #4706 stopgap |
| New CI jobs | `static-checks` (15 min budget), 4 test jobs (20 each), `orchestrate-poll` × 4 groups (20 each), `lint` aggregate (5) |
| Orchestrate-poll split | 4 matrix groups × `CI_POLL_TEST_SHARDS` (default 4) local shards |
| First split run (run 36523765261) | 9.0 minutes wall-clock; critical path `orchestrate-poll (0)` at 8.7 minutes, slowest test job `tests-promote-stall-and-review` at 8.2 minutes |
| Release `validate-scripts` | 37 minutes measured (run 36374918973); budget 45 → 60 minutes |

What this means for operators: CI results arrive in about 9 minutes instead of most of an hour. That shortens every Claude-fixer review round, and clean reviews are more likely to find a ready check snapshot within `CHECK_RUNS_WAIT_TIMEOUT_SECS`. The Checks tab shows each job separately; `CI / lint` still summarises them.

### For contributors

Add a new CI step to one of the existing jobs. A new job must also go into the `lint` job's `needs` list, which `tests/test_ci_job_split_contract.py` enforces. `tests/test_ci_poll_test_sharding.py` checks that the group split and the local shard split, alone and together, run every orchestrate-poll test exactly once.
