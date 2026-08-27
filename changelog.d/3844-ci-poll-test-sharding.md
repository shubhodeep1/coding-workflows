<!-- changelog: fixed -->
- **CI finishes again.** The `CI / lint` job was being cancelled at its 30-minute timeout on every run, including on `main`, so the repository had no completing full-test gate. The orchestrate-poll module now runs across four parallel shards and the job budget is 45 minutes.

`tests/test_orchestrate_poll_process.py` is the critical path. Most of the 307 tests in its post-fast-fail sharded subset spawn the real poller as a bash subprocess inside a throwaway sandbox, so they cost seconds each rather than milliseconds — 71 of the first 95 took over 4 seconds, the slowest 27. Run sequentially the module took roughly 35 minutes on a 4-core box, more than the entire job was allowed, so CI never reached the steps after it and every run ended `cancelled`. Each test allocates its own tempdir sandbox, so the module shards with no shared state and no harness change: the runner already accepts test names as arguments.

| The numbers that matter | Value |
| --- | --- |
| New repo var | `CI_POLL_TEST_SHARDS` (default `4`) |
| Job timeout | 30 → 45 minutes |
| Measured speedup on 4 cores | 3.6x (62s → 17s on a 16-test slice) |
| Tests in the sharded CI subset | 307 |
| New test methods | 12 in `tests/test_ci_poll_test_sharding.py` |

What this means for operators: CI reaches its final steps and reports a real result instead of being cut off mid-suite, so a green tick again means the suite passed. Set `CI_POLL_TEST_SHARDS=1` to fall back to sequential; a non-numeric or non-positive value warns and does the same.

### For contributors

The split is `NR % total == n`, and `tests/test_ci_poll_test_sharding.py` extracts that expression from the workflow rather than restating it, then proves it is a true partition across shard counts 1, 2, 3, 4, 5, and 8 and against the module's live test count. A copy of the expression in the test would have agreed with the step only at authoring time; extracting it means a change to the split is exercised. Shards are all waited on before any is judged, so one early failure cannot orphan its siblings on the runner, and a shard whose exit code was never recorded is treated as failed rather than passing silently.
