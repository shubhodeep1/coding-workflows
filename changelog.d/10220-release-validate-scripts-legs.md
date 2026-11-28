<!-- changelog: changed -->
- **The release gate's `validate-scripts` job now runs its two long suites side by side, and the CI budget watch warns before a gate job runs out of time.**

One week after `validate-scripts` in `.github/workflows/test-and-mark-stable.yml` got 135 minutes, it again used most of them: run 45927857867 (2026-11-27) took 125 minutes (orchestrate-poll tests 70.5, unit tests 51.1) and run 46009933351 took 109. The job is now a two-leg matrix: the `orchestrate-poll` leg runs the orchestrate-poll module and the `unit-tests` leg runs every other check, so the job takes as long as the longer leg instead of the sum. The job id, step names and limit are unchanged, and `validate` still needs both legs to pass.

`scripts/ci_budget_watch.py` (the `budget-watch` job of `ci.yml`) now also reads the latest completed `test-and-mark-stable.yml` run and opens one `ai:ci-budget` issue for any job at 75% or more of its `timeout-minutes`, with subject `test-and-mark-stable.yml/<job>`.

| The numbers that matter | Value |
| --- | --- |
| Worst serial run (45927857867) | 125 of 135 minutes (93%) |
| Expected longest leg at that run | about 72 minutes (53%) |
| Runners for `validate-scripts` per gate run | 2 instead of 1 |
| Extra API reads per budget-watch run | 2 |

What this means for operators: the daily release gate stops failing only because this job's two suites grew past the limit together, and a gate job near its limit now opens an issue before it times out.
