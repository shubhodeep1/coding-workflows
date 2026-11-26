<!-- changelog: added -->
- **A daily audit of every issue and pull request the pipeline touched, with root-cause fix plans and one blocked-items report.**

Nothing checked whether the issues and pull requests the pipeline created were needed, were resolved the right way, or were stuck. The 06:00 UTC run of `workflow-log-analysis.yml` now audits every issue and PR updated since the previous run, in every repository the pipeline account can reach (plus this one), from a per-repository cursor in `analysis/issue_pr_audit_state.json`. The first run looks back 24 hours.

Rules flag likely waste (closed unmerged PRs, not-planned or duplicate issues, reverts, follow-up fixes, reopen, review churn, no-op changes) and blocked items (`ai:needs-human` and the other blocking labels, stalled pipeline items, conflicting or failing PRs). The model reviews the flagged items, a sample of clean ones and newly blocked items, and writes `analysis/issue-pr-audit-<date>.md` with one `AUD-###` fix plan per distinct cause, citing existing `ai:workflow-heal` issues and earlier plans instead of planning a cause twice. The report reaches the default branch through a generated-chore PR that skips the reviewer panel, and the daily hand-off passes it to the orchestrator. One Telegram message a day lists every blocked item across all repositories.

| The numbers that matter | Value |
| --- | --- |
| New jobs | 0 (steps in the existing `collect-logs` job; timeout 60 -> 100 minutes) |
| API calls per run | about 2 REST per active repository, 1 GraphQL per 25 items, up to 20 for blocked-item logs, 3 more |
| Model calls per run | 1, plus 1 retry when the report fails its structure check |

Refs #10153

What this means for operators: `ISSUE_PR_AUDIT_ENABLED=false` turns the audit off; `APPLY_ANALYSIS_DOC_GLOB_EXTRA=none` keeps the reports but stops handing them to the orchestrator. Private repositories appear in the committed report only as `private:<hash>` while this repository is public.
