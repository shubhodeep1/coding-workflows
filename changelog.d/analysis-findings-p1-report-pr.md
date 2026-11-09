<!-- changelog: changed -->
- **The workflow log analysis report now arrives through a pull request.** The `Commit and push report` step of `workflow-log-analysis.yml` no longer pushes the new `analysis/workflow-optimization-<date>(-N).md` report straight to the dispatch branch.

The step now keeps the purge, the commit and the retry-with-rename loop as they were, but pushes to a run-unique `auto/workflow-log-analysis-report-<run_id>-<attempt>` branch. It opens a PR against the dispatch branch (the body is linted by `scripts/lint_pr_body_auto_close.py` first), then tries a squash merge with `--match-head-commit`, then `--auto`, and otherwise leaves the PR open with a warning. It never uses `--admin`, so branch protection still applies. The step writes new outputs `delivery`, `report_pr_number`, `report_pr_url` and `report_branch` next to the unchanged `committed` and `report_file`, and logs one `WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY` line. The analyze step's report path now reaches the shell through `env:` and must match the dated report pattern. `analyze-commit-notify` gets a job-level `pull-requests: write` grant, `deep-audit` runs only when the report reached the branch, and the Telegram message links the PR while the report waits in it.

| The numbers that matter | Value |
| --- | --- |
| GitHub API calls per committed run | at most 3 (`gh pr create`, `gh pr merge`, `gh pr merge --auto`), plus 1 `gh pr list` only when the PR already exists |
| New repo variable | `WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR` (default `true`) |

What this means for consumer repos: after the next `@stable` sync, each dispatched log analysis run opens a report PR. Where branch protection refuses the direct merge, the report lands when auto-merge completes or someone merges the PR, and that run skips the deep audit. Set `WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR=false` to keep the direct push.

Refs #9116
