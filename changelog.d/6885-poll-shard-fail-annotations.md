<!-- changelog: changed -->
- **A failing orchestrate-poll test shard now names its failing tests, or prints the end of its log, in `::error::` annotations, in CI and in both release gates.**

Before, a failed shard only reported `orchestrate-poll shard N failed (exit R)`. The test that failed was printed inside the shard's collapsed `::group::` log, which check-failure-triage cannot read. Issue #6885 (CI run 37907020014) could therefore not say which test broke. The shard judgment loop in `ci.yml`, `mark-stable.yml` and `test-and-mark-stable.yml` now adds one `::error::orchestrate-poll shard N: FAIL  <test>: <reason>` annotation per runner `FAIL` line, up to 20. If a failed shard has no `FAIL` line (for example, it was interrupted), the loop prints an `::error::… had no FAIL line` annotation followed by the shard's last 20 log lines, each prefixed with `orchestrate-poll shard N | `, so log text cannot start a workflow command.

What this means for operators: the failing test name now shows up in the run's annotations and job log tail. Exit codes, the shard failure count and the gate result are unchanged.
