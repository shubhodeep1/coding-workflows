<!-- changelog: changed -->
- **A failed orchestrate-poll test shard now names its failing tests.** The shard-judgment loop in CI's `orchestrate-poll` job and in the `validate-scripts` job of both release gates (`mark-stable.yml`, `test-and-mark-stable.yml`) turns each failed shard's runner `FAIL` lines into `::error::` annotations and lists all of them in plain text just before the final tally.

Before this change a failed shard raised only `orchestrate-poll shard N failed (exit N)`. The test runner's `  FAIL  <name>: <error>` line was printed only inside the collapsed shard log group, so check-failure triage of run 37906796258 (issue #6883) could name the shard but not the test. Each failed shard now contributes up to 20 of its `FAIL` lines, each cut to 500 characters, with `%` and carriage returns escaped as GitHub workflow commands require. A failed shard whose log has no `FAIL` line gets a `no FAIL line in log (runner or fixture error)` annotation instead. Exit codes and the existing error lines are unchanged.

| The numbers that matter | Value |
| --- | --- |
| `FAIL` lines collected per failed shard | at most 20 |
| Characters kept per line | 500 |
| Test-name annotations per step | at most 5 (the plain-text list holds the rest) |
| Workflow copies changed | 3 |

What this means for operators: a red `orchestrate-poll` job now shows the failing test names in the run's annotations and at the end of the job log, so there is no need to expand each shard's log group to find them.

### For contributors

`tests/test_ci_poll_test_sharding.py` runs the shipped judgment loop from all three workflows against sample shard logs. It checks the exact escaped annotation, the fallback annotation, the 5-annotation cap, the 20-line and 500-character limits, and that a passing shard adds nothing.
