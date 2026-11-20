<!-- changelog: fixed -->
- **The deep audit and API redundancy sections of the workflow log analysis report are delivered again.** Since the report started arriving through its own pull request, the two section jobs of `workflow-log-analysis.yml` committed their sections locally and then dropped them (`outcome=skipped reason=no_report_branch`), because no report branch reached them.

Now, once the report is on the dispatch branch (pushed directly, or its PR merged and its branch deleted), `deep-audit` re-creates the run-unique `auto/workflow-log-analysis-report-<run_id>-<attempt>` branch from the branch tip that holds the report and pushes its section there; `api-redundancy` continues on that branch (or re-creates it when the deep audit added nothing). Pushes are fast-forward only and never go to the default branch. The `deliver-report` job then opens one sections-only pull request through `scripts/workflow_log_analysis_deliver_report.sh`, which in this mode does not rename the report (it is on the branch by design), checks that the recorded base is an ancestor of both the dispatch branch and the run branch, and accepts only changes to the report file itself. The PR body is linted by `scripts/lint_pr_body_auto_close.py` first, and `--admin` is never used.

| The numbers that matter | Value |
| --- | --- |
| GitHub API calls in the section jobs | 0 (git only) |
| GitHub API calls in `deliver-report`, sections-only mode | 1 `gh pr create`, plus 1 `gh pr list --head` only when the create fails (the open-PR listing is skipped) |
| GitHub API calls in `deliver-report`, full-report mode | unchanged: 1 paginated open-PR listing, 1 `gh pr create`, 1 `gh pr list --head` only when the create fails |
| New repo variable | `WORKFLOW_LOG_ANALYSIS_REPORT_SECTIONS_VIA_PR` (default `true`) |

What this means for consumer repos: after the next `@stable` sync, a dispatched log analysis run whose report reached the branch opens a second, sections-only pull request carrying the deep audit and API redundancy sections; CI and the review workflow merge it like the report PR. A run branch left over from a failed deletion makes the section push fail with `reason=push_failed`; delete the branch and re-run the job. Set `WORKFLOW_LOG_ANALYSIS_REPORT_SECTIONS_VIA_PR=false` to keep the previous behaviour (sections stay local, with a warning).

Refs #9116
