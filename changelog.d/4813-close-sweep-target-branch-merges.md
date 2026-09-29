<!-- changelog: fixed -->
- **A merged PR now finishes an issue only when it merged into that issue's target branch.** Two paths closed or labelled issues on any merged PR with a closing keyword: the orchestrator poller's `close_merged_issues_sweep` and `issue_pr_status.yml`. Both now ignore a merge into an unrelated branch.

On 2026-09-28, completion PR #4748 merged into the project branch of issue #4688, a branch that is neither `main` nor the `Integration branch:` #4688 names. The body fallback in `issue_pr_status.yml` read the prose "closes #4688" as a closing keyword and labelled the issue `ai:merged` (run 36411092888). Six minutes later, `close_merged_issues_sweep` in `scripts/orchestrate_poll_process.sh` closed #4688 because of that label, while its project still had to merge into the parent project's branch. A merged PR now counts only when its base is one of three branches: the repository's default branch; the branch the issue names on its `Integration branch:` or `Target branch:` line; or any branch for an orchestrator-managed child issue. A merge into any other branch leaves the issue's labels and state alone. The sweep logs it as `CLOSE_MERGED_SWEEP … rejected=non_target_base` and applies its existing no-merged-PR policy for that label class.

| The numbers that matter | Value |
| --- | --- |
| Branches that count | default branch, the issue's `Integration branch:` / `Target branch:`, any base for an orchestrator-managed child |
| New GitHub API calls | 0: the base comes from the `pulls/<n>` payload already fetched, and the issue body from the existing `gh issue list` calls and GraphQL payloads |
| Default branch in `issue_pr_status.yml` | read from the event payload (`main` when absent), replacing the hardcoded `main` |
| Repos affected | this repo, and the consumers in `.github/ai/consumer_repos.json` on the next `@stable` sync |

What this means for operators: the Claude issue chain and orchestrator projects no longer lose their issue partway through a project whose final base is another project branch. Orchestrator child issues still close when their PR merges into `orchestrator/project-<N>`, and Codex-built security follow-ups still get `ai:merged` when their PR merges into the project branch they name. A PR closed without merging behaves as before.

### For contributors

`issue_body_integration_branch` in `scripts/gh_helpers.sh` is the shell parser for the issue's target branch. It uses the same grammar as `extract_integration_branch` in `scripts/orchestrate_lib.py`, and `tests/test_gh_helpers_issue_body_integration_branch.py` checks that the two agree. `tests/test_issue_pr_status_target_branch_gate.py` runs the real workflow step against a stub `gh`. The new sweep cases are in `tests/test_orchestrate_poll_process.py`.
