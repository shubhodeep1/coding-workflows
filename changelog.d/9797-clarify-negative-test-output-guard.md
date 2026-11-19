<!-- changelog: fixed -->
- **The release gate's clarify negative test no longer fails when a GitHub API read is rate-limited.** Its run-id step outputs now hold only digits.

Release gate run 44490333814 failed in "Create unsolvable issue" after the issue had been created. A failed `gh api` request prints its JSON error body to stdout, so the step's `|| echo "0"` fallback did not replace it, and the multi-line body was written to `GITHUB_OUTPUT` as `pre_clarify_run_id`. The runner rejected the file as `Invalid format`. Now, in the `clarify-rejects-unsolvable-test` job of `.github/workflows/test-and-mark-stable.yml`:

- "Create unsolvable issue" writes `pre_clarify_run_id=0` with a warning when the read is not a number, and stops with an `::error::` instead of writing a malformed `issue_number`.
- "Capture clarify run id (negative test)" treats a non-numeric read as no run yet and keeps polling within its 90-second limit, and reads a non-numeric `PRE` as `0`.

The captured text is never echoed. No GitHub API calls are added.

The same run's Phase 7 failure (the cancel-on-close run stayed queued for the whole 10-minute wait while the runner pool was full) is environmental and is not changed here.

### For contributors

Tests: `tests/test_test_and_mark_stable_clarify_negative_outputs.py` runs both step bodies against a fake `gh` that returns a rate-limit body.
