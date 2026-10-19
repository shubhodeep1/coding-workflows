<!-- changelog: changed -->
- **Two test steps moved out of the CI job `orchestrate-poll (2)`.** That matrix group took 19m28s of its 20-minute timeout on `main` (run 39245970945, budget-watch issue #7697). A job that reaches its timeout turns CI red even when the code is fine. Both steps had moved into group 2 in #7689. They run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Promote cycle and blocked-verdict unit tests` | `tests-release-and-log-analysis` |
  | `Review-blocked judge label propagation contract test` | `tests-promote-stall-and-review` |

  The step timings could not be read when this was planned. The two jobs were chosen from earlier recorded durations, as the jobs with the most estimated headroom, and each takes one step because each was flagged while it carried both. Neither job's new duration has been measured; the next `budget-watch` run on `main` checks every job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-release-and-log-analysis` or `tests-promote-stall-and-review`. Operators have nothing to do.
