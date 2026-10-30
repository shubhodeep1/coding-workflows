<!-- changelog: fixed -->
- **The release gate now reports a cancel-on-close run that never finishes as a timeout, and no longer shows it as skipped.** This brings the #8327 reporting fix to `stable`.

In release gate run 40982514785, Phase 7 closed smoke PR #8423 and found cancel-on-close run #41000719920, but that run was still not `completed` when the 10-minute `PHASE7_WAIT_BUDGET_MINUTES` budget ran out. The step printed `concluded ` with an empty conclusion and wrote an empty `status=`, and the results table then printed `Cancel-PR .. SKIPPED (no PR to close)` and "ALL PHASES PASSED" while the job failed. Phase 7 in `.github/workflows/test-and-mark-stable.yml` now logs `cancel-on-close run #<id> did not complete within <n>m (status=<status>)` and writes `status=timeout` and `cancel_run_id`, as its existing-run path already did. The results table prints `FAILED (no status ...)` and fails the gate when a smoke PR existed but Phase 7 reported no outcome; it still prints SKIPPED when there was no smoke PR.

What this means for operators: the gate still fails in this case, but now says why. It may keep failing with `timeout` until the change that gives each PR-close run its own concurrency group in `cancel_on_pr_close.yml` is promoted from `main`. The wait budget is unchanged.

### For contributors

Tests: `tests/test_test_and_mark_stable_phase7_cancel_timeout_status.py` (runs the timeout branch and the results row with stubbed inputs), wired into the release gate's `validate-scripts` Unit tests step.
