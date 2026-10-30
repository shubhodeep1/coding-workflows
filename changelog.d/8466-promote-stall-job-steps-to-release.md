<!-- changelog: changed -->
- **Three test steps moved out of the CI job `tests-promote-stall-and-review`.** That job took 15m33s of its 20-minute timeout on `main` (run 41042353484, budget-watch issue #8466). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run, at the end of the release job:

  | Step | New job |
  |---|---|
  | `Validation bootstrap and family direct-run tests` | `tests-release-and-log-analysis` |
  | `Review/judge/conflict strict-render contract test` | `tests-release-and-log-analysis` |
  | `Review synthesize-smoke contract test` | `tests-release-and-log-analysis` |

  `tests-release-and-log-analysis` has had no budget-watch issue of its own since #8278, which moved its largest step out, and #8268 cut its collector coverage gate from about 5 minutes to seconds. The other test jobs had more recent issues (#8281, #8394, #8395). The bootstrap step arrived in `tests-promote-stall-and-review` from `static-checks` in #8394 and now moves on again. The run's step timings could not be read, so the steps were picked by estimated cost (test and subprocess counts), not measured, and the release job's remaining headroom is likewise an estimate. If either job comes near its timeout, the next `budget-watch` run on `main` opens an issue for it that links the run's step timings. `tests/test_ci_poll_test_sharding.py` now checks that each moved step runs once, in its new job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-release-and-log-analysis`. Operators have nothing to do.
