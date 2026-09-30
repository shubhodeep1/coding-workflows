<!-- changelog: fixed -->
- **The release gate's E2E smoke job no longer rejects a genuine review run because its job log was still empty.** An empty or whitespace-only `codex-agent` log now counts as "not readable yet", and the smoke job reads it again (#5689).

`smoke_review_checked_out_sha` in `scripts/smoke_review_dispatch.sh` reads a review run's `codex-agent` job log to find the commit the run checked out. GitHub can return an empty log body for a job that has only just finished, while its log is still being stored. The helper read that as "the log has no checkout line" (return code 2). Phase 4 of `.github/workflows/test-and-mark-stable.yml` then rejected the Bug B run for the rest of the step, and Phase 4b ended with `retry_run_unverified`. So the gate could fail on a timing race. An empty or whitespace-only body now returns 1, which both phases already retry: Phase 4 on its next poll, Phase 4b every 15 seconds until its retry deadline. Return code 2 still means a non-empty log with no checkout line, and the first matching line still wins.

| The numbers that matter | Value |
| --- | --- |
| Return code for an empty or whitespace-only log body | 1, retryable (was 2, rejected) |
| Extra GitHub API calls per check | 0 |

What this means for operators: a smoke run that hits the window where GitHub has not stored a job log yet now waits for the log instead of failing the release gate. A log that stays empty never verifies the run: Phase 4 never counts it, and Phase 4b still ends with `retry_run_unverified` at its deadline.

### For contributors

The check is `grep -aq '[^[:space:]]'` on the temp file the helper already writes. It reads the file directly rather than through a pipe, so a caller's `set -o pipefail` cannot turn a non-empty log into a false "empty". `tests/test_smoke_review_dispatch.py` covers an empty body, a whitespace-only body, a CRLF-only body, a non-empty body with no line, and a genuine line.
