<!-- changelog: changed -->
- **The release gates' `validate-scripts` job now has 135 minutes instead of 90.**

The v1.47.6 attempt of `test-and-mark-stable.yml` (run 44689548109, 2026-11-20) was cancelled at 90 minutes in its "Unit tests" step. The orchestrate-poll module took 52.7 minutes, and the unit tests needed about 45 more, for roughly 100 minutes in all. The 2026-11-17 run 44093205767 had already used 88 of the 90 minutes. The `validate-scripts` jobs of `.github/workflows/test-and-mark-stable.yml` and `.github/workflows/mark-stable.yml` now allow 135 minutes, which keeps at least 25% headroom over the measured runtime.

| The numbers that matter | Value |
| --- | --- |
| Old budget | 90 minutes |
| New budget | 135 minutes |
| Measured runtime (run 44689548109) | about 100 minutes |

What this means for operators: a release no longer fails only because its static and unit test job ran out of time while every test was passing. The job still runs on one runner, in parallel with the live E2E jobs, so the release's total time is unchanged unless this job is the last to finish.
