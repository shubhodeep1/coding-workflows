<!-- changelog: added -->
- **A PR body line `Implements #N` now links a person's PR to an orchestrator item issue.** Before, the poller accepted a PR as an item's implementation PR only when its head branch was `ai/issue-<n>` or its body closed the issue, so a person who finished a blocked item and wrote `Refs #N` saw the PR rejected as `not_implementation_pr` and the item stayed `ai:blocked` (fun-token-multi-chain#639, PR #667).

`_pr_json_is_issue_implementation_pr` in `scripts/orchestrate_poll_process.sh` now also accepts a body line `Implements #N`, `Implements <owner>/<repo>#N` or `Implements https://github.com/<owner>/<repo>/issues/N` for this repository and this exact issue number, when the PR's base is the default branch or an `orchestrator/project-<n>` branch. `Implements` is not a closing keyword, so it never auto-closes a tracking issue. A bare `Refs #N` is still rejected, which keeps the #3817 protection against adopting a PR that only mentions the issue. Each marker decision logs `LINKED_PR_IMPLEMENTS_MARKER`.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | 0 (the default branch comes from the memoized `_review_run_default_branch`) |
| Accepted bases | default branch, `orchestrator/project-<n>` |
| Kill switch | `ORCH_IMPLEMENTS_MARKER_ENABLED=false` (default `true`, wired in `orchestrate_poll.yml`) |

What this means for operators: when you finish an orchestrator item yourself, put `Implements #N` in the PR body and the poller picks the PR up after it merges. Set the repository variable `ORCH_IMPLEMENTS_MARKER_ENABLED` to `false` to turn the rule off; items already closed through it stay closed.

### For contributors

The CLAUDE.md §19 bullet that documents the convention for interactive sessions could not be written by the pipeline; the exact text is recorded as a pending operator step in `agents.d/7765-implements-marker.md` (`IMPLEMENTS_MARKER_CLAUDE_MD_UNSET_OPERATOR_STEP`).
