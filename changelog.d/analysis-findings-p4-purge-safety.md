<!-- changelog: changed -->
- **The workflow log analysis purge keeps unprocessed docs, and the stable gate analyses every repo since the last report.** Three changes to `workflow-log-analysis.yml` and one to `test-and-mark-stable.yml`:

- **Purge safety.** The `Commit and push report` step still removes `analysis/workflow-optimization-<date>(-N).md` docs older than `WORKFLOW_LOG_ANALYSIS_REPORT_RETENTION_DAYS`, but now only the ones the recommendation processing report (`APPLY_ANALYSIS_REPORT_PATH`, default `analysis/recommendation-processing-report.md`) lists. An unprocessed doc is kept and logged `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=<path> reason=unprocessed`. When the processing report is missing or unreadable, nothing is purged (`reason=processing_report_unavailable`). The in-flight guard from #7022 still runs first.
- **`since_last_report` window.** A new dispatch input, `since_last_report` (default `false`), starts the collection window at the largest `window_end` found in the first-line marker of an analysis doc added in the last 8 days. It reads that history with git only, so it makes no API call, and clamps the window to 7 days. Without a usable marker it falls back to `lookback_days`, and an explicit `since` still wins. When the input is set, the step logs one `WORKFLOW_LOG_ANALYSIS_WINDOW` line. The `collect-logs` job now has `window_start` and `window_end` outputs.
- **Window marker.** The analyze step writes `<!-- workflow-log-analysis: window_start=<ts|unknown> window_end=<ts> run=<id> -->` as each report's first line, above the data-gap line. A `repos_override` run covers only some repositories, so its report gets no marker and a full-roster run never starts at its window end.
- **Gate coverage.** The stable gate now dispatches the analysis with `since_last_report=true` instead of `repos_override=TEST_REPO`, so it covers the full repo roster. It does this only when `TEST_REPO` is this repository and `WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS` (new, default `true`) is on.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | 0 |
| Longest `since_last_report` window | 7 days |
| New repo variable | `WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS` (default `true`) |

What this means for consumer repos: `analysis/` keeps every doc until an apply-analysis run records it in the processing report. A repo with no processing report keeps all its reports. Set `WORKFLOW_LOG_ANALYSIS_GATE_ALL_REPOS=false` to keep the gate's single-repo, one-day analysis.

Refs #9116
