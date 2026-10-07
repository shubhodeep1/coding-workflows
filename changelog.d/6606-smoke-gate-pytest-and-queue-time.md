<!-- changelog: fixed -->
- **The release smoke gate no longer fails before it starts, and runner queue time no longer counts against the Phase 4b retry.** Both of the last two nightly main→stable promotion cycles failed in `test-and-mark-stable.yml`, which kept `stable` at 2026-10-03.

"Phase 0a: Hot orchestrate-poll regression guard" runs `tests/test_orchestrate_poll_process.py` with the runner's `python3`, and that file has imported pytest since #6187. Gate run 37554001238 failed there with `ModuleNotFoundError: No module named 'pytest'` before any phase ran. Phase 0a now installs pytest when it is missing, as Phase 4b already did, and reports `status=pytest_install_failed` if it cannot. In gate run 37395952357 the Phase 4b retry review run waited for a hosted runner (`status=queued`) for most of its 25-minute `EDITOR_RETRY_BUDGET_MINUTES` window and succeeded six minutes late. Queued time seen on consecutive polls now extends that deadline, capped so the job still leaves Phase 6, Phase 7 and the finalization reserve inside `E2E_JOB_TIMEOUT_MINUTES`.

| The numbers that matter | Value |
| --- | --- |
| Seconds into the 2026-10-07 gate when Phase 0a failed | 10 |
| Retry run queued time in the 2026-10-06 gate | about 24 of 25 minutes |
| Latest retry deadline with default budgets | 240 minutes after the job starts (300 − 30 − 10 − 20) |
| Change to `E2E_JOB_TIMEOUT_MINUTES` or any job timeout | none |

What this means for operators: the nightly `promote-main-to-stable.yml` cycle can get past the smoke gate again, so consumer repos can receive the work merged since 2026-10-03 once a proving cycle completes. A Phase 4b `retry_timeout` now logs how many seconds the run spent queued and how far the deadline was extended.

### For contributors

The job start is recorded as `E2E_JOB_STARTED_EPOCH` in the e2e job's "Validate prerequisites" step; without it Phase 4b adds no extension. Tests: `tests/test_test_and_mark_stable_review_blocked_budget.py` (`test_phase4b_queued_retry_run_extends_the_deadline`, `test_phase4b_queue_extension_needs_a_recorded_job_start`, `test_phase4b_queue_extension_stops_at_the_job_budget_ceiling`, `test_phase0a_installs_pytest_before_running_the_hot_poller_test`).
