<!-- changelog: fixed -->
- **Planning closes a CI-repair issue whose pull request already merged with the failing check green.**

Issue #7185 asked the pipeline to repair CI on PR #6976 after that PR had merged with CI passing, and planning proposed edits to the PR's former head branch. Before the planning model runs, `plan.yml` now checks check-failure triage issues (label `ai:check-triage`) and pipeline re-issues that trace back to one. If the pull request merged and the named check passed on its final head, the issue gets one comment naming the PR and check, the `ai:closed` label, and is closed as not planned. If the pull request merged but the check did not pass, planning goes ahead against the PR's base branch, with the merged-PR facts in the planner's context. Any read failure, untrusted author or unclear lineage plans as usual (#7216).

| The numbers that matter | Value |
| --- | --- |
| `PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED` default | `true` |
| Re-issue hops followed / issues read, at most | 4 / 8 |
| API reads per qualifying issue | 1 identity, up to 8 issues, 1 PR, 1 or 2 check reads |

What this means for operators: nothing to set. A closed issue can be reopened if the check was wrong; set `PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED=false` to turn the check off.
