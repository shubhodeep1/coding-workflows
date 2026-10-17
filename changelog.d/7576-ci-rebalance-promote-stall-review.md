<!-- changelog: changed -->
- **Two slow test steps moved out of the CI job `tests-promote-stall-and-review`.** That job took 15m18s of its 20-minute timeout on `main` (run 39039638823, budget-watch issue #7576), and a job that hits its timeout turns CI red with no code fault. The steps run unchanged and under the same names:

  | Step | New job |
  |---|---|
  | `Review autofix review-pipeline plumbing contract test` | `tests-heal-plan-and-validation` |
  | `Regression tests wired by the CI wiring guard (part 2)` | `tests-heal-plan-and-validation` |

  Step timings could not be read when this was planned, so the choice rests on estimates from test and subprocess counts: these were the job's two most expensive steps, about 2.7 and 2.1 minutes. With those estimates the source job drops to about 10 to 11 minutes and `tests-heal-plan-and-validation` stays under its 20-minute timeout. The next `budget-watch` run on `main` checks it.

  No job was added or renamed, no `timeout-minutes` changed, and the aggregate `CI / lint` check is unchanged. A failure in one of these tests now appears under the `tests-heal-plan-and-validation` check-run. Operators have nothing to do.
