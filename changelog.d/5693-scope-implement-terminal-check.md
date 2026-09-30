<!-- changelog: fixed -->
- **The release smoke test no longer fails Implement while the smoke issue's own Implement run is still running.** Before `test-and-mark-stable.yml` declares Implement finished without a PR, it now checks the smoke issue's own Implement runs, not a sample of whatever ran recently.

Run 36717635823 failed `Phase 3b: Wait for PR creation (implement phase)` with `All 1 implement workflow run(s) completed but no PR was created`. The step counted Implement runs on one unscoped page of the newest 50 runs. By then the smoke issue's own run, `36720184070`, had slid past that page, and the one completed run left on it was the parallel alt-model job's cancelled run. The step failed, cleanup closed the smoke issue, and the real run finished with `success` 2.5 minutes later. The page of 50 is now only a progress signal. Before failing, the step walks the same window 100 runs per page for non-skipped Implement runs titled like the smoke issue, and fails only when that walk reached the end of the window with none of them active. An unreadable page, or a walk that hits the page cap, counts as unknown: the step keeps waiting, still bounded by `PHASE_TIMEOUT`. A genuine failure still fails with `status=implement_failed`, and the error line now names the smoke issue's newest Implement run and its conclusion, or says that no Implement run ran for the issue.

| The numbers that matter | Value |
| --- | --- |
| Runs read by the progress poll every `POLL_INTERVAL` | 50 (one page, unchanged) |
| Pages read by the new check before failing | up to 10 of 100 runs, stopping at the first page with an active run of ours or at a short page |
| Reads per later check while our run is active | 1 (the cached run, by ID) |
| Status and error prefix on a genuine failure | `implement_failed`, `All <n> implement workflow run(s) completed but no PR was created` (unchanged) |

What this means for release operators: a busy repository no longer fails the stable release gate at Implement because another smoke job's run finished first. When Implement really fails, the gate's error line gives the smoke issue's Implement run ID and conclusion to open.

### For contributors

The change is in the `wait-implement` step: the helper `summarize_scoped_impl_runs` and the cycle-local cache `IMPL_SCOPED_ACTIVE_RUN_ID`. It reuses `SCOPED_RUN_LOOKUP_MAX_PAGES` and the `created=>${IMPL_CREATED_AFTER}` window from the run-ID capture. `tests/test_test_and_mark_stable_plan_polling_guard.py` runs the real step script against a stubbed `gh` and a stub `date` clock. It covers the incident shape (our run active on page 2 behind another issue's completed run), a genuine failure, no run for the issue, an unreadable page, the page cap, and the cached run completing.
