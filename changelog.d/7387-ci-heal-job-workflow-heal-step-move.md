<!-- changelog: changed -->
- **The workflow failure heal tests moved from the CI job `tests-heal-plan-and-validation` to `tests-promote-stall-and-review`.** The heal job took 15m12s of its 20-minute timeout on `main` (run 38584572151, budget-watch issue #7387), and a job that hits its timeout turns CI red with no code fault. Earlier rebalances moved other steps out and the job stayed near 15 minutes, so this change moves its heaviest step, `Workflow failure heal contract and driver tests`, unchanged and under the same name. The promote job is the only test job that has never received moved steps or been flagged by budget-watch.

  Step timings were not readable when this was planned, so the step was picked by test count and subprocess count. No job was added or renamed, no `timeout-minutes` changed, and the aggregate `CI / lint` check is unchanged. A failure in the workflow-heal or provider-outage tests now appears under the `tests-promote-stall-and-review` check-run.

  What this means for operators: nothing to change; no new variable.
