<!-- changelog: changed -->
- **Approved PRs stop cycling through repeated base updates.** A freshness update no longer waits for a new security audit, its final CI run starts right away, and new Python test files no longer edit `ci.yml`.

On 2026-11-16, 58 PRs were open, and approved ones were updated from `main` up to five times without merging. Nearly every forced update (18 of 19 sampled) overlapped on `.github/workflows/ci.yml`, because the CI wiring guard required each new test file to be named there. Each update head then waited, one step after another, for a single-issue security audit, a review run and a full 37-job final CI run, and `main` moved again first. Now `tests/test_ci_wires_every_test_file.py` counts every `tests/test_*.py` file as run by the `tests-system-python` catch-all. A verified freshness update of a cleanly audited head reuses that clean result. The merge path that makes the update dispatches the final CI run immediately, and that run is a quick one (static checks, the PR's own tests and the CI-structure tests) when the overlap is limited to CI-wiring files and the approved head already passed a full run. A failed quick run never sends the PR back to review: the `final-ci-failed` job dispatches the full suite for that head instead.

| The numbers that matter | Value |
| --- | --- |
| Sampled updates that overlapped on `ci.yml` (#9283, #9367, #9368, #9389, #9453) | 18 of 19 |
| Most updates on one PR (#9367) | 5 |
| Security audits saved per update of a clean head | 1 |
| Test jobs skipped in a sync-quick run | 7 of 9 worker jobs |
| API calls per update for the early dispatch | up to 3 PR reads, 1 runs read for the quick check, then the final-CI helper's runs read, PR-files read and 1 dispatch (2 when a quick dispatch is refused) |

What this means for operators: expect fewer `Branch updated from main before merging` comments per PR, no `Single-issue security pass` audit on update heads whose approved head was clean, and final CI runs named `CI [pr:<n>] [sync-quick]`. The full suite still runs on `main` after every merge. `MERGE_BASE_SYNC_FINAL_CI_ENABLED=false` and `MERGE_BASE_SYNC_QUICK_CI_ENABLED=false` turn off the early dispatch and the quick mode.

### For contributors

New scripts and helpers: `scripts/ci_sync_quick.sh` (`classify`, `full-ci-green`, `verify`, `run-tests`), `_pr_base_sync_dispatch_final_ci` and the `PR_FINAL_CI_SYNC_QUICK` input of `_pr_ensure_final_ci` in `scripts/pr_checks_lib.sh`, and `REVIEW_HEAD_GATE_FRESHNESS_SYNC_FROM` from `review_head_gate_freshness_sync`. ci.yml gains the `sync_quick` dispatch input and the `tests-sync-quick` job, which `lint` needs. Do not add a new Python test file to `ci.yml`; a `tests/test_*.sh` file still needs a workflow entry.
