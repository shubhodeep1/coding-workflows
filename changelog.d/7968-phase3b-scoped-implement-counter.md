<!-- changelog: fixed -->
- **The release gate's Phase 3b now sees this issue's Implement run on a busy repository, and reports a run that ended without a PR as `implement_failed` instead of a 30-minute stall.**

The progress counter in "Phase 3b: Wait for PR creation (implement phase)" of `test-and-mark-stable.yml` used to read one unscoped page of 50 runs. In run 39926633796 the smoke issue's Implement run was not on that page, so the counter reported `impl_runs: 0` throughout. The title-scoped check that names the run and its conclusion therefore never ran, and the step ended with `status=timeout`. The counter (`scoped_implement_progress_counts`) now applies the selection and validation rules of `scoped_implement_runs_complete`: only non-skipped Implement runs titled with this issue's title count. It walks up to 10 pages of 100 runs, stops at the first page with a match, and re-reads at most every 30 seconds. An unreadable or malformed page leaves the counts at 0 / `unknown`, as before. When the issue goes back from `ai:implementing` to `ai:awaiting-approval`, the step logs the latest such run's ID, status and conclusion. Timeouts, the grace period and the pass/fail rules are unchanged.

The same run's orchestrate-decompose timeout had a different cause: the dispatched run sat `queued` for about 41 of its 50 minutes because no runner was free, then finished successfully. That timeout got no code change.

### For contributors

`tests/test_test_and_mark_stable_plan_polling_guard.py` adds a regression test for an Implement run behind a full page of unrelated runs, plus tests for other issues' runs and the label-return log line.
