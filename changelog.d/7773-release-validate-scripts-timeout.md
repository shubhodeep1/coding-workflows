<!-- changelog: fixed -->
- **The release gate's `validate-scripts` job now has 90 minutes instead of 60.** Slow script tests no longer cancel the job and block a release.

The v1.37.0 attempt (`test-and-mark-stable.yml` run 39526632477) passed every test it finished. The `validate-scripts` job was cancelled at its 60-minute cap partway through "Unit tests", so `validate` and `release` were skipped and no tag was created. The orchestrate-poll suite alone took 39.5 minutes on that runner, and the release before it (run 39213880700) had already used 56 of the 60 minutes. Both release gates, `test-and-mark-stable.yml` and `mark-stable.yml`, now allow 90 minutes. No test was removed or skipped.

| The numbers that matter | Value |
| --- | --- |
| `validate-scripts` cap | 60 → 90 minutes |
| Orchestrate-poll step, run 39526632477 | 39.5 minutes |
| `validate-scripts`, run 39213880700 | 56 of 60 minutes |

What this means for operators: a release no longer fails because the script tests ran slowly on the runner. A failing test still blocks it.
