<!-- changelog: changed -->
- **Two slow test steps moved out of the CI job `tests-release-and-log-analysis`.** That job took 20m16s of its 20-minute timeout on `main` (run 39030079651, budget-watch issue #7560), and a job that hits its timeout turns CI red with no code fault. The steps run unchanged and under the same names:

  | Step | New job |
  |---|---|
  | `Merge-train and gh_helpers rate-limit tests (CLAUDE.md §15)` | `tests-hooks-and-orchestrator` |
  | `Check-failure triage workflow security tests` | `tests-promote-stall-and-review` |

  The merge-train step goes back to the hooks job it left on 2026-10-09; that job has had room since #7366 moved six steps out of it. Step timings could not be read when this was planned, and most of these test modules cannot run without PyYAML, so the choice rests on estimates from test and subprocess counts: about 4.5 minutes for the merge-train step (its measured time on 2026-10-09) and 1.5 to 2 minutes for the triage step. With those estimates the release job drops to about 14 minutes and both receiving jobs stay at about 15 minutes or less. The next `budget-watch` run on `main` checks it.

  No job was added or renamed, no `timeout-minutes` changed, and the aggregate `CI / lint` check is unchanged. A failure in one of these tests now appears under its new job's check-run. Operators have nothing to do.
