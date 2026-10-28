<!-- changelog: changed -->
- **Four more test steps moved out of the CI job `tests-release-and-log-analysis`.** That job took 15m13s of its 20-minute timeout on `main` (run 40716905879, budget-watch issue #8268). A job that reaches its timeout turns CI red even when the code is fine. The steps run unchanged and keep their names, once per CI run:

  | Step | New job |
  |---|---|
  | `Set up Node 22 for the claude-pool-broker Worker tests` | `tests-heal-plan-and-validation` |
  | `claude-pool-broker Worker tests (vitest)` | `tests-heal-plan-and-validation` |
  | `Workflow log analysis contract tests` | `tests-heal-plan-and-validation` |
  | `Semantic cache unit tests` | `tests-heal-plan-and-validation` |

  No job is known to have room. `tests-heal-plan-and-validation` took 15m06s of its 20 minutes on `main` (run 40626853481, before #8246 split its largest step into 4 parallel processes). `tests-hooks-and-orchestrator` gave up the Node pair in #7798 and three steps in #8179 because it was full, and `tests-promote-stall-and-review` took steps in #8179 and #8245. The run's step timings could not be read, so the steps and the destination were picked by estimated cost, not measured. If the destination job comes near its timeout, the next `budget-watch` run on `main` opens an issue for it (unless one is already open) that links the run, whose step timings show what to move. The Node setup and the vitest step move together, in that order, and none of the four needs git history. `tests/test_ci_poll_test_sharding.py` now checks that each moved step runs once, in its new job.

  On this PR's first CI run, `tests-release-and-log-analysis` still reached its timeout. Its `Workflow log collector coverage gate` step took about 5 minutes for unit tests that finish in about a second. The cause was `test_main_log_output_dir_writes_categorized_full_logs_and_dedupes_downloads` in `tests/test_collect_workflow_logs.py`. It ran the collector without `AI_MEMORY_ENABLED=false`, so in CI the collector cloned the real `ai-memory` branch from GitHub and tried to push a cache to it with the job's read-only token. The test now turns memory off, as the other collector test that runs the script already did. It no longer touches GitHub or the shared branch, and the step should drop to seconds.

  No job was added or renamed, no `timeout-minutes` changed, and the `CI / lint` check works as before. A failure in one of these tests now shows up under the check-run `tests-heal-plan-and-validation`. Operators have nothing to do.
