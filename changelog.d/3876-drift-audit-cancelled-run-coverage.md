<!-- changelog: fixed -->
- **The nightly drift audit no longer fires a daily partial-coverage WARNING for cancelled review runs.** Absent logs on completed runs concluded `cancelled` or `skipped` are now classified as expected instead of missing.

The daily `.github/workflows/drift-audit.yml` run scans the last 24 hours of `internal-review.yml` and `review_autofix.yml` logs, and almost every day some of those runs were concurrency-cancelled by a newer push before uploading any logs. `scripts/drift_audit.sh` counted each one as missing coverage, flipped the run to `partial`, and escalated the per-run Telegram summary to WARNING — for example the 2026-08-29 run reported "fetched 62, missing 11" with all 11 missing IDs being cancelled runs. The audit now records those runs as `unscannable` (a new `DRIFT_AUDIT_COVERAGE` field, run-summary key, and "Cancelled/skipped runs without logs" job-summary row), keeps coverage `full`, and the alert stays at DEBUG. A failed log fetch for any other conclusion still reports `partial` coverage and a WARNING alert, and cancelled or skipped runs whose logs do exist are still scanned for drift markers.

| The numbers that matter | Value |
| --- | --- |
| Missing-log runs in the 2026-08-29 audit, all cancelled | 11 of 73 scanned |
| Conclusions treated as expected-no-logs | `cancelled`, `skipped` |
| Alert level for a cancelled-only gap (was WARNING) | DEBUG |

What this means for operators: the ⚠️ "Partial log coverage" Telegram alert now only fires when a log that should exist could not be fetched, so a WARNING from the drift audit is worth reading again instead of daily noise.

### For contributors

The run-summary JSON gains `logs_unscannable` / `unscannable_run_ids`, appended after the existing fields; the shell wrapper's jq TSV appends the new count last so existing field positions are unchanged. `_fetch_run_log` takes a `log_expected` keyword argument that downgrades the fetch-failure annotation from `::warning::` to an info line for cancelled/skipped runs.
