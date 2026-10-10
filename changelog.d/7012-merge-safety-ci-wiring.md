<!-- changelog: changed -->
- **Auto-merge now waits for CI runs that are still queued, CI warns before it outgrows its budgets, and every test file runs in CI.** A pull request can no longer pass the merge gate before its CI run has started, and 66 test files that no workflow ran are now checked on every push.

The required-checks wait in `scripts/pr_checks_lib.sh` used to see only check-runs that already existed. A queued CI run has none yet, so a head could pass before CI started. The wait now also reads the head's workflow runs and keeps waiting while a run of a workflow listed in `AUTO_MERGE_WAIT_WORKFLOWS` is not completed; a timeout then reports `reason=ci_run_pending`. A new `budget-watch` job in `ci.yml` opens one `ai:ci-budget` issue when a workflow file or a CI job nears its limit, so the split lands before the guard or the timeout breaks CI. `tests/test_ci_wires_every_test_file.py` fails when a `tests/test_*` file runs in no workflow.

| The numbers that matter | Value |
| --- | --- |
| `AUTO_MERGE_WAIT_WORKFLOWS` | `CI` (default; `none` or empty disables) |
| `CI_BUDGET_WORKFLOW_WARN_BYTES` | `440000` (guard: 480,000) |
| `CI_BUDGET_JOB_WARN_RATIO` | `0.75` of `timeout-minutes` |
| Test files that ran in no workflow | 66 of 315, 7 of them stale |
| Final-merge gate test file | timed out at 600 s, now 39 s |

What this means for operators: a merge waits for CI to actually run on the head, and CI size and duration problems arrive as ordinary pipeline issues before they turn CI red.

### For contributors

Add a new test file to an existing CI step of the job whose topic it covers. A file that cannot run in CI needs an `UNWIRED_ALLOWLIST` entry with its reason. Tests that source `scripts/orchestrate_poll_process.sh` should set `AI_MEMORY_ENABLED=false` unless they test the ai-memory cache, or each test fetches the ai-memory branch from origin.
