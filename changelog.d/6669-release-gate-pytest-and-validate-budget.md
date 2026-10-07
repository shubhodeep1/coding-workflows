<!-- changelog: fixed -->
- **The release gate's hot poller check now installs `pytest` before it runs, and the standalone validate check has time to report its own timeout.**

`Test & Mark Stable Release` run 37624152181 failed twice. `Phase 0a: Hot orchestrate-poll regression guard` ran the poller test module before anything in the E2E job installed `pytest`, so it stopped with `ModuleNotFoundError: No module named 'pytest'`. The step now installs `pytest` with the same pip-then-apt sequence Phase 4b uses, checks the import, and fails closed with `status=pytest_unavailable` and the install log when neither works. Separately, the `validate-standalone-test` job's 30-minute cap matched the watcher's 1,800-second wait, so GitHub cancelled the job while its child validate run was still in progress and before the watcher could report. The dispatch step now has a 35-minute cap and the job a 45-minute cap, which leaves time for the soft-error analyser. The watcher's waits are unchanged, and why that child run stayed in progress is not yet known.

What this means for operators: a gate run no longer fails Phase 0a on a missing test dependency, and a stuck standalone validate run now fails with the watcher's own `timed out` message and an analysed log instead of a bare cancellation.

### For contributors

`test_hot_poller_guard_installs_and_verifies_pytest_before_running` and `test_validate_standalone_job_budget_exceeds_watcher_wait` in `tests/test_test_and_mark_stable_review_blocked_budget.py` pin the install order and the step and job caps.
