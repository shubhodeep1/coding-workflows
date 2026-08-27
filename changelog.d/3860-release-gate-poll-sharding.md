<!-- changelog: fixed -->
- **Release runs no longer fail by timeout while every test is passing.** The `validate-scripts` job in both release gates now shards the orchestrate-poll test module instead of running it serially.

Release v1.27.0 (run 33073743283) failed with zero test failures: the serial unit-test step in `test-and-mark-stable.yml` hit the job's 30-minute cap and was cancelled mid-suite, which skipped `validate` and `release`. The cause was the one PR #3844 had already fixed in `ci.yml` — `tests/test_orchestrate_poll_process.py` spawns the real poller as a subprocess per test and alone consumed ~24 of the 30 budgeted minutes — but the release gates in `mark-stable.yml` and `test-and-mark-stable.yml` were never sharded. Both now run the module across `CI_POLL_TEST_SHARDS` workers (default 4, the same repository variable ci.yml uses) with the same verified `NR % total == n` partition, and their job budgets rise from 30 to 45 minutes for cold-runner headroom.

| The numbers that matter | Value |
| --- | --- |
| Failed release run | 33073743283 (v1.27.0) |
| Poll-module share of the 30-minute budget | ~24 minutes serial |
| Shard count | `CI_POLL_TEST_SHARDS`, default 4 |
| `validate-scripts` budget | 30 → 45 minutes |

What this means for operators: a release run is cancelled only if something is genuinely wrong, not because the test suite grew; a red `validate-scripts` again means a failing test. Setting `CI_POLL_TEST_SHARDS` now affects the release gates as well as `CI / lint`.

### For contributors

`tests/test_ci_poll_test_sharding.py` pins the ported step in both release workflows (partition expression, failure handling, reap-before-judge ordering, serial-invocation removal, 45-minute budget), so the three copies of the shard split cannot silently diverge.
