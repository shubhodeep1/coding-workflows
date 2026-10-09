<!-- changelog: fixed -->
- **A failing orchestrate-poll test shard now names its failing tests at the end of the CI step.** The shard step in `ci.yml`, `mark-stable.yml` and `test-and-mark-stable.yml` repeats each failing shard's first five `  FAIL  <test>: <error>` lines as `::error::` lines after every shard's log group, with an `and N more FAIL line(s)` count past five. A shard that failed without any FAIL line gets a `no FAIL line in its log` error and its last 30 log lines instead.

Each shard's log group is printed in shard order, so a failure in shard 1 sat above shards 2 and 3 and fell out of the log tail. CI run 37915639128 on PR #6904 showed only `orchestrate-poll shard 1 failed (exit 1)` in the tail, and two check-failure triage generations (issue #6919) could not name the failing test. Repeated lines drop carriage returns, are cut to 300 characters and escape `%` as `%25`; pass/fail still comes only from each shard's exit code, and the existing error lines are unchanged.

| The numbers that matter | Value |
| --- | --- |
| FAIL lines repeated per failing shard | 5 |
| Characters per repeated line | 300 |
| Log tail for a shard without a FAIL line | 30 lines |

What this means for operators: the next shard failure names its test in the job annotations and the log tail, so triage can reproduce it instead of reporting the cause as inconclusive. PR #6904 itself still needs a CI rerun.
