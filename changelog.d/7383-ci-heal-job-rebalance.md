<!-- changelog: changed -->
- **Three slow test steps moved from the CI job `tests-heal-plan-and-validation` to `tests-release-and-log-analysis`.** The heal job took 15m19s of its 20-minute timeout on `main` (run 38564033998, budget-watch issue #7383), and a job that hits its timeout turns CI red with no code fault. The release job is the only one with measured headroom (535-604s). These steps now run there, unchanged and under the same names:
  - `Orchestrator judges on the Claude engine tests (Phase 5c)`
  - `Validation harness sandbox tests`
  - `Check-failure triage workflow security tests`

  Step timings were not readable when this was planned, so the steps were picked as the heal job's most subprocess-heavy ones. No job was added or renamed, no `timeout-minutes` changed, and the aggregate `CI / lint` check is unchanged. A failure in one of these tests now appears under the `tests-release-and-log-analysis` check-run.

  What this means for operators: nothing to change; no new variable.
