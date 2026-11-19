<!-- changelog: fixed -->
- **Workflow log analysis no longer opens a report PR that cannot merge when `GH_PAT` is missing.** In PR mode (`WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR`, default `true`) the `Commit and push report` step (`id: commit_report`) now fails before any report push or PR-delivery `gh` call when the `GH_PAT` secret is unset, logging `::error::WORKFLOW_LOG_ANALYSIS_REPORT_DELIVERY outcome=failed reason=token_missing branch=<run branch>`. Before, it fell back to `github.token`, and a PR opened with that token starts no required checks and stays open. The report is still kept as artifact `workflow-log-analysis-report`, and Telegram sends the existing failed-delivery WARNING. Push mode (`WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR=false`) keeps the `github.token` fallback. Step name, id, `if` and outputs are unchanged. A new runtime test also covers a GH013 ruleset refusal of the run-branch push (`reason=push_rejected_by_ruleset`).

What this means for consumer repos: a repo that runs workflow log analysis without `GH_PAT` now sees a failed delivery step instead of a stuck report PR. Set `GH_PAT`, or set `WORKFLOW_LOG_ANALYSIS_REPORT_VIA_PR=false`.

Refs #9116
