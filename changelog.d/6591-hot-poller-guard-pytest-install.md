<!-- changelog: fixed -->
- **The release gate now installs pytest before its early orchestrate-poll guard.** The `Test & Mark Stable Release` smoke job runs `main`'s `tests/test_orchestrate_poll_process.py` in "Phase 0a: Hot orchestrate-poll regression guard". The job installed pytest only later, in Phase 4b, so once that test began importing pytest the guard failed with `ModuleNotFoundError: No module named 'pytest'` before any smoke phase ran (run 37513567927). A new step, "Phase 0a: Install hot orchestrate-poll guard dependencies", now installs pytest into the runner's system `python3` first, using the same pip-then-apt pattern as Phase 4b. If pytest still cannot be imported, the step fails and the summary reports the guard as not run, so the release stays blocked. The guard itself is unchanged.

  | Item | Value |
  |---|---|
  | Workflow fixed | `.github/workflows/test-and-mark-stable.yml`, job `e2e-smoke-test` |
  | New step id | `hot-poller-guard-deps` |
  | Install log | `${RUNNER_TEMP}/hot_poller_guard_deps.log` (printed only on failure) |
  | Contract test | `tests/test_test_and_mark_stable_hot_poller_guard_deps.py` (4 tests, run by `ci.yml`) |
