<!-- changelog: security -->
- **Text in an issue body can no longer make an issue look orchestrator-managed and get it closed by an unrelated merge.** `close_merged_issues_sweep` and `issue_pr_status.yml` now require the automation-applied `ai:orchestrator-managed` label. A managed child also finishes only when its PR merges into its own project branch.

Both paths skipped the target-branch check for orchestrator-managed child issues and closed them on a merge into any branch. They counted an issue as managed when its body contained the text `Managed by: AI Orchestrator` anywhere, even in prose. So a standalone issue that quoted the marker could be closed by a closing-keyword PR merged into an unrelated branch. The security audit reported this as issue #4957 against `scripts/orchestrate_poll_process.sh`. Now an issue counts as managed only with the label. A managed child's merged PR then counts only when its base is the default branch, the child's `Integration branch:`, or `orchestrator/project-<T>` from its `Tracking issue: #<T>` line. Any other merge leaves the issue alone. The sweep logs it as `CLOSE_MERGED_SWEEP … rejected=non_target_base … project_base=<branch>`, and the workflow logs it with the child's integration and project branches.

| The numbers that matter | Value |
| --- | --- |
| Signals that make an issue managed for closing | 1: the `ai:orchestrator-managed` label (was: the label or the body text) |
| Branches a managed child's merge counts on | default branch, its `Integration branch:`, its `orchestrator/project-<T>` (was: any branch) |
| New GitHub API calls | 0 |
| Repos affected | this repo, and the consumers in `.github/ai/consumer_repos.json` on the next `@stable` sync |

What this means for operators: orchestrator child issues close as before, because the orchestrator creates every child with the label and its metadata lines. An issue that only mentions the marker text is now treated like any standalone issue. Its merged PR finishes it only on the default branch or on the branch its own `Integration branch:` / `Target branch:` line names. The merged-PR Telegram alert still treats the marker text as managed and stays quiet for such issues.

### For contributors

`issue_body_orchestrator_project_branch` in `scripts/gh_helpers.sh` reads the `Tracking issue: #<T>` line in plain or bold form. It prints nothing when that line is missing or names two different tracking issues. `issue_pr_status.yml` records each labelled issue's project branch in `ISSUE_MANAGED_PROJECT_BRANCHES`, from the GraphQL payloads it already fetches or from its per-issue REST fallback. The new cases are in `tests/test_issue_pr_status_target_branch_gate.py`, `tests/test_orchestrate_poll_process.py`, and `tests/test_gh_helpers_issue_body_integration_branch.py`.
