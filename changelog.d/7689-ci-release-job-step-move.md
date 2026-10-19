<!-- changelog: changed -->
- **Four test steps moved out of the CI job `tests-release-and-log-analysis`.** That job took 17m51s of its 20-minute timeout on `main` (run 39230958248, budget-watch issue #7689). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names; each runs in one group of the `orchestrate-poll` matrix, once per CI run:

  | Step | New job (group) |
  |---|---|
  | `Promote cycle and blocked-verdict unit tests` | `orchestrate-poll (2)` |
  | `Review-blocked judge label propagation contract test` | `orchestrate-poll (2)` |
  | `Orchestrator judges on the Claude engine tests (Phase 5c)` | `orchestrate-poll (3)` |
  | `Validation harness sandbox tests` | `orchestrate-poll (3)` |

  The step timings could not be read when this was planned. The steps were therefore chosen by estimated cost: they are the job's most subprocess-heavy steps, the ones moved in by #7383 and #7677. Groups 0 and 1 already run extra steps, so these went to groups 2 and 3. Neither group's new duration has been measured; the next `budget-watch` run on `main` checks every job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `orchestrate-poll (2)` or `orchestrate-poll (3)`. Operators have nothing to do.
