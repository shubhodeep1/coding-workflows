<!-- changelog: fixed -->
- **Workflow log analysis reports reach `main` again, through a pull request.** The purge now deletes only analysis docs the apply-analysis loop has already processed.

Since 2026-10-26 the default-branch ruleset requires the `lint` and `ai-review/head-gate` checks and has no bypass actors. `.github/workflows/workflow-log-analysis.yml` still pushed its report straight to `main`, so every dispatched run failed at "Commit and push report" with GH013 (for example run 42318132836), and no analysis doc landed after `analysis/workflow-optimization-2026-10-25.md`. Each run now pushes its report to `auto/workflow-log-analysis-report-<run_id>-<run_attempt>`, and the deep-audit and API-redundancy sections are appended on that branch. A new `deliver-report` job then runs `scripts/workflow_log_analysis_deliver_report.sh` with GH_PAT. The script renames the report if its path is taken on the base branch or on another open report PR's branch, lints the PR body for auto-close keywords, and opens one PR that CI and the review workflow merge. It logs `WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=pr_opened`, and it fails closed (`reason=token_missing`) when GH_PAT is unset, because a PR opened with `github.token` starts no CI.

The 30-day purge used to delete docs nobody had processed yet: commit `0fcc49b2` lost the 09-24 docs, and the failed 11-07 run would have deleted five more. It now keeps a doc unless its basename is listed in `analysis/recommendation-processing-report.md`, the same test `unprocessed_docs()` uses, and logs `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=<path> reason=unprocessed`. When the processing report is missing, nothing is purged.

| What changed | Before | After |
| --- | --- | --- |
| Report delivery | direct push to `main` (GH013) | per-run branch + one PR |
| Purge | every doc older than the retention window | only docs listed in the processing report |
| New job outputs on `analyze-commit-notify` | none | `committed`, `report_branch`, `purged_docs` |

### For contributors

Tests: `tests/test_workflow_log_analysis_report_delivery.py`, collected by `tests/test_workflow_log_analysis_failure_contract.py`. Refs #8963.
