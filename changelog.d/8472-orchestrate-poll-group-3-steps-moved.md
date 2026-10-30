<!-- changelog: changed -->
- **CI: the `Validation self-test unit tests` step moved from `orchestrate-poll (3)` back to `static-checks` (#8472).** Group 3 took 19m30s of its 20-minute `timeout-minutes` on `main` (run 41071474282). A job that reaches its timeout is cancelled and turns CI red with no code fault. Group 3 had stayed under budget-watch's warning line until it gained this step (#8394) and `Orchestrate poll single-file module tests` (#8396) together, and this step is the slower of the two.

  | Step | Old location | New location |
  |---|---|---|
  | `Validation self-test unit tests` | `orchestrate-poll`, group 3 | `static-checks` |
  | `Orchestrate poll single-file module tests` | `orchestrate-poll`, group 3 | unchanged (group 3) |

  This step was first moved to `tests-promote-stall-and-review`, which was cancelled at its 20-minute timeout on the pull request's own CI run (41097621008) while the step still had twelve modules to finish. Every 20-minute job already sits near budget-watch's warning line. `static-checks` is the only job with a measured bound: it took 11m17s with this step and two other test steps (run 40954844196), so with this step alone it stays under its 15-minute timeout, though it may cross budget-watch's 11m15s warning line. The step name and commands are unchanged; only the `strategy.job-index == 3` guard was dropped, because `static-checks` has no matrix. No job was added or renamed, no `timeout-minutes` changed, and `CI / lint` is unchanged. A failure in these tests now shows under `static-checks` instead of `orchestrate-poll (3)`. Operators have nothing to do.
