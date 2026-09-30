<!-- changelog: security -->
- **An unrelated PR can no longer close an issue by merging into its integration branch.** Off the default branch, `close_merged_issues_sweep` and `issue_pr_status.yml` now count a merged PR as an issue's finished work only when the PR comes from an automation branch for that issue in the same repository.

Before this change, a merged PR whose body said `Fixes #<n>` and whose base matched the `Integration branch:` line in the issue body (a line the issue author can edit) was accepted as the issue's implementation. `issue_pr_status.yml` then labelled the issue `ai:merged`, or closed it outright for an orchestrator-managed child, and the sweep closed it. Now the head must be `ai/issue-<n>`, `ai/issue-<n>-…` / `ai/issue-<n>/…`, or the orchestrator judge's `fix/<n>-followup-<epoch>`, and it must live in this repository. The sweep logs any other candidate as `CLOSE_MERGED_SWEEP … rejected=unverified_identity` and falls through to its existing no-merged-PR policy. Merges into the default branch are unchanged, because GitHub closes the issue on those anyway.

| The numbers that matter | Value |
| --- | --- |
| Source issue | #5226 (security audit finding, severity high) |
| Accepted heads off the default branch | `ai/issue-<n>`, `ai/issue-<n>-…`, `ai/issue-<n>/…`, `fix/<n>-followup-<epoch>` |
| New GitHub API calls | 0 (head data comes from the existing `pulls/<n>` read and the event payload) |

What this means for operators: an issue whose only merged PR into a project or integration branch came from a hand-named branch or a fork is no longer closed automatically. If it carries `ai:merged`, the sweep raises its existing stale-label Telegram warning, and a human closes it. Claude issue-mode projects close their own issues at their final merge and are not affected.

### For contributors

The shared predicate is `pr_head_ref_is_issue_automation_branch` in `scripts/gh_helpers.sh`. `issue_pr_status.yml` defines a fail-closed stub for it when an older `gh_helpers.sh` lacks it, and reads the head repository from `PR_HEAD_REPO_FULL_NAME` (`github.event.pull_request.head.repo.full_name`). Tests: `tests/test_gh_helpers_issue_automation_branch.py`, plus new cases in `tests/test_issue_pr_status_target_branch_gate.py` and `tests/test_orchestrate_poll_process.py`.
