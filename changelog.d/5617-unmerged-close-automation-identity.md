<!-- changelog: security -->
- **A PR closed without merging no longer closes an issue it merely links.** `issue_pr_status.yml` now labels `ai:closed`, closes, and finalizes lineage on an unmerged close only for the issue's own automation PR.

Before this change, when any PR closed without merging, the `Update linked issue labels when PR closes` step in `.github/workflows/issue_pr_status.yml` labelled every issue the PR linked `ai:closed`, closed it, and wrote its AI-memory lineage as `closed`. The link comes from `Fixes #<n>` in the PR body, which the PR's author controls, so in a run with `GH_PAT` anyone could close an arbitrary issue by opening and closing a PR. The security audit reported this as issue #5617. The step now applies the automation-identity check that merges into non-default branches already use (issue #5226): the PR's head must be in this repository and be an automation branch for that issue (`ai/issue-<n>`, `ai/issue-<n>-…`, `ai/issue-<n>/…`, or `fix/<n>-followup-<epoch>`). Any other head logs `PR #<p> closed without merging is not an automation PR for issue #<n> (head …, head repo …); leaving its labels, state, and lineage unchanged.` The same check now gates lineage finalization for orchestrator-tracking issues and for issues whose classification lookups failed.

| The numbers that matter | Value |
| --- | --- |
| Heads that still close an issue on an unmerged close | same-repository `ai/issue-<n>…` and `fix/<n>-followup-<epoch>` for that issue |
| Unchanged | every merged-PR path (#4813, #4957, #5226, #5227), the Telegram alert and cleanup steps |
| New GitHub API calls | 0 |

What this means for operators: an issue is closed by an abandoned PR only when that PR was the pipeline's own implementation of it. A hand-written PR that says `Fixes #<n>` and is closed without merging leaves #<n> open, as GitHub itself does. Consumer repos get the fix through the `ai-issue-pr-status.yml` wrapper on the next `@stable` sync, with no variable to set.

### For contributors

The check is the `pr_is_issue_automation_pr` shell function in the step, built on `pr_head_ref_is_issue_automation_branch` from `scripts/gh_helpers.sh`. An older helper copy without that function hits the step's fail-closed stub, so unmerged closes then change no issue. Tests: `tests/test_issue_pr_status_target_branch_gate.py`.
