<!-- changelog: changed -->
- **The test step `Regression tests wired by the CI wiring guard (part 1)` moved from the CI job `tests-release-and-log-analysis` to `tests-heal-plan-and-validation`.** `tests-release-and-log-analysis` took 15m33s of its 20-minute timeout on `main` (run 38663329196, budget-watch issue #7406), and a job that hits its timeout turns CI red with no code fault. The heal and validation job got the step because #7387 split that job's slowest step into parallel shards, so it should have the most room.

  The step runs there unchanged and under the same name. No job was added or renamed, no `timeout-minutes` changed, and the aggregate `CI / lint` check is unchanged. A failure in these tests now appears under the `tests-heal-plan-and-validation` check-run. Both jobs should now take about 12 minutes or less, but that is an estimate from test-module and subprocess counts, not a measurement; the next `budget-watch` run on `main` checks it.

  What this means for operators: nothing to change; no new variable.
