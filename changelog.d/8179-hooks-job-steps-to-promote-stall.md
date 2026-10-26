<!-- changelog: changed -->
- **Three test steps moved out of the CI job `tests-hooks-and-orchestrator`.** That job took 15m01s of its 20-minute timeout on `main` (run 40264887710, budget-watch issue #8179). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Orchestrate lib unit tests` | `tests-promote-stall-and-review` |
  | `Judge Semble prefetch contract test` | `tests-promote-stall-and-review` |
  | `Standalone clarify auto-decide tests (port P3)` | `tests-promote-stall-and-review` |

  `tests-promote-stall-and-review` was chosen because it has the most room: its last budget-watch issue was #7677, and six steps have moved out of it since. The steps were picked by test and subprocess counts, because the run's step timings could not be read. None of them needs git history or the `.claude/` live copies that only `tests-hooks-and-orchestrator` prepares. These durations are estimates, not measurements; the next `budget-watch` run on `main` checks every job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-promote-stall-and-review`. Operators have nothing to do.
