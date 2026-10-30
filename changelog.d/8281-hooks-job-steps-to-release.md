<!-- changelog: changed -->
- **Four test steps moved out of the CI job `tests-hooks-and-orchestrator`.** That job took 15m27s of its 20-minute timeout on `main` (run 40752698170, budget-watch issue #8281). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run, before the release job's coverage gates:

  | Step | New job |
  |---|---|
  | `Claude engine selection and claude_run tests` | `tests-release-and-log-analysis` |
  | `Single-issue security pass tests (port P1)` | `tests-release-and-log-analysis` |
  | `Review sandbox Claude role tests (Phase 5c)` | `tests-release-and-log-analysis` |
  | `Session automation retirement guard and retired labels/files tests` | `tests-release-and-log-analysis` |

  `tests-release-and-log-analysis` gave up fourteen steps in #8208, #8245, #8268 and #8278, and #8268 also fixed the collector test that made its collector coverage gate take about 5 minutes. It took four steps from `tests-heal-plan-and-validation` in #8279, so with this change it has received eight. The other test jobs took steps in #8179, #8208, #8245, #8268 and #8278; `tests-hooks-and-orchestrator` itself took one step in #8278. The guard-hook, live-copy parity and pre-push gate steps stay in `tests-hooks-and-orchestrator`, because they need the `.claude/` live copies only that job prepares. The run's step timings could not be read, so the steps were picked by estimated cost (test and subprocess counts), not measured, and the release job's remaining headroom after both moves is likewise an estimate. If either job comes near its timeout, the next `budget-watch` run on `main` opens an issue for it that links the run's step timings. `tests/test_ci_poll_test_sharding.py` now checks that each moved step runs once, in its new job.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-release-and-log-analysis`. Operators have nothing to do.
