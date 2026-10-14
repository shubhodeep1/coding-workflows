<!-- changelog: changed -->
- **The test step `Regression tests wired by the CI wiring guard (part 1)` moved from the CI job `static-checks` to `tests-release-and-log-analysis`.** `static-checks` took 15m18s of its 15-minute timeout on `main` (run 38562929820, budget-watch issue #7382), and a job that hits its timeout turns CI red with no code fault. The release and log-analysis job had the most room. The step runs there unchanged and under the same name.

  No job was added or renamed, no `timeout-minutes` changed, and the aggregate `CI / lint` check is unchanged. A failure in these tests now appears under the `tests-release-and-log-analysis` check-run.

  What this means for operators: nothing to change; no new variable.
