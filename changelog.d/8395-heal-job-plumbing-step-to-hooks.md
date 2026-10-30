<!-- changelog: changed -->
- **One test step moved out of the CI job `tests-heal-plan-and-validation`.** That job took 15m10s of its 20-minute timeout on `main` (run 40954844196, budget-watch issue #8395). A job that reaches its timeout turns CI red even when the code is fine. The step runs unchanged and keeps its name, once per CI run:

  | Step | New job |
  |---|---|
  | `Review autofix review-pipeline plumbing contract test` | `tests-hooks-and-orchestrator` |

  This step was picked because it is the job's largest step that is not already split into parallel processes: its nine modules hold about 360 tests, and its first module alone makes 126 subprocess calls. `tests-hooks-and-orchestrator` was chosen because it has the most room: #8281 moved four steps out of it, and since then it has only taken the small renderer coverage gate (#8278). The run's step timings could not be read, so the pick and the destination's headroom are estimates, not measurements; the next `budget-watch` run on `main` checks every job. The step needs only checkout, Python 3.12 and the shared pip install, and runs after the hooks job's `Install Python CI dependencies` step.

  The heal job keeps its two parallel-sharded steps (`Workflow failure heal contract and driver tests` and `Regression tests wired by the CI wiring guard (part 1)`) and the steps that earlier moves (#8208, #8268) pinned to it. `tests/test_ci_poll_test_sharding.py` now checks that the moved step runs once, in `tests-hooks-and-orchestrator`, after its setup step.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-hooks-and-orchestrator`. Operators have nothing to do.
