<!-- changelog: fixed -->
- **The release gate's clarify negative test no longer fails when a GitHub API read is rate-limited.** Its run-id step outputs now hold only digits.

Release gate run 44490333814 failed in "Create unsolvable issue" after the issue had been created. A failed `gh api` request prints its JSON error body to stdout, so the step's `|| echo "0"` fallback did not replace it, and the multi-line body was written to `GITHUB_OUTPUT` as `pre_clarify_run_id`. The runner rejected the file as `Invalid format`. Now, in the `clarify-rejects-unsolvable-test` job of `.github/workflows/test-and-mark-stable.yml`:

- "Create unsolvable issue" writes `pre_clarify_run_id=0` with a warning when the read is not a number, adds `pre_clarify_run_id_read_failed=1`, and stops with an `::error::` instead of writing a malformed `issue_number`.
- "Capture clarify run id (negative test)" treats a non-numeric poll read as no run yet and keeps polling within its 90-second limit. When the baseline read failed, or `PRE` is not a number, it skips the poll with a warning. The soft-error analyser is then skipped instead of analysing an older, unrelated clarify run.

The same guards now cover the `e2e-alt-model-test` job. "Create alt-model smoke issue" leaves `pre_<phase>_run_id` empty when a read fails, and stops with an `::error::` when `issue_number` is not a number. "Capture alt-model phase run ids" ignores non-numeric run ids. It skips clarify and plan, which have no branch filter, when their baseline is empty.

The captured text is never echoed. No GitHub API calls are added.

The same run's Phase 7 failure (the cancel-on-close run stayed queued for the whole 10-minute wait while the runner pool was full) is environmental and is not changed here.

### For contributors

Tests: `tests/test_test_and_mark_stable_clarify_negative_outputs.py` runs the step bodies of both jobs against a fake `gh` that returns a rate-limit body.
