<!-- changelog: fixed -->
- **Workflow log analysis reports reach `main` again, through a pull request.** The purge now deletes only analysis docs the apply-analysis loop has already processed.

Since 2026-10-26 the default-branch ruleset requires the `lint` and `ai-review/head-gate` checks and has no bypass actors, so every dispatched run of `.github/workflows/workflow-log-analysis.yml` failed at "Commit and push report" with GH013 (for example run 42318132836). No analysis doc landed after `analysis/workflow-optimization-2026-10-25.md`. Each run now pushes its report, and the deep-audit and API-redundancy sections, to `auto/workflow-log-analysis-report-<run_id>-<run_attempt>`. A new `deliver-report` job then opens one PR for that branch, which CI and the review workflow merge.

The 30-day purge used to delete docs nobody had processed yet: commit `0fcc49b2` lost the 09-24 docs, and the failed 11-07 run would have deleted five more. It now keeps a doc unless its basename is listed in `analysis/recommendation-processing-report.md`, the same test `unprocessed_docs()` uses, and logs `WORKFLOW_LOG_ANALYSIS_PURGE_SKIPPED doc=<path> reason=unprocessed`. When the processing report is missing, nothing is purged.

| What changed | Before | After |
| --- | --- | --- |
| Report delivery | direct push to `main` (GH013) | per-run branch + one PR |
| Purge | every doc older than the retention window | only docs listed in the processing report |
| New job outputs on `analyze-commit-notify` | none | `committed`, `report_branch`, `purged_docs` |

What this means for operators: report PRs named `chore: add workflow log analysis report (...)` now appear after each dispatched run, and the run log names the PR. A run fails at "Deliver report pull request" when GH_PAT is unset, when it was dispatched from a ref other than the default branch, or when the run branch changes anything besides analysis docs.

### For contributors

`scripts/workflow_log_analysis_deliver_report.sh` renames the report when its path is taken on the base branch or another open report PR's branch, lints the PR body for auto-close keywords before any push, and logs `WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=pr_opened`. It fails closed with `reason=token_missing` (a PR opened with `github.token` starts no CI), `reason=run_context_invalid` (non-default ref) or `reason=unexpected_paths`. The three pushing jobs check out with `persist-credentials: false`; their fetch and push steps use a one-shot credential helper that reads `GH_TOKEN` from the step env. The PR body reports a section as completed only when its job succeeded.

Tests: `tests/test_workflow_log_analysis_report_delivery.py`, collected by `tests/test_workflow_log_analysis_failure_contract.py`. Refs #8963.
