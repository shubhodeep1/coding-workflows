<!-- changelog: fixed -->
- **Stall recovery no longer closes unrelated pull requests that merely mention a stalled issue.** Only the issue's own implementation PR is closed.

When orchestrator stall recovery decides to close and re-issue a stuck task, `close_linked_pr` in `scripts/orchestrate_poll_process.sh` enumerates linked PRs from three sources, one of which is the issue's timeline cross-references. GitHub records a cross-reference for any PR whose body mentions `#<issue>`, and the `Refs #<issue>` linkage this repo requires produces exactly that event, so a rule-following tooling fix that cited a stalled issue was treated as the issue's implementation PR and closed unmerged. Every open candidate now passes through `_linked_pr_is_issue_implementation`: it is closed only when its head branch follows the orchestrator convention (`ai/issue-<n>`, `ai-implement-<n>`) or its body carries a GitHub close keyword for the issue. Cross-reference-only PRs stay open and are logged with a new `close_linked_pr: skipping PR #<pr> … (cross-reference only; …)` line. The check reads head, base, and body from the same `pulls/<n>` request that already read the PR state, so it adds no API calls.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls per candidate PR | 0 |
| Lookup strategies still consulted | 3 (timeline, branch name, body keyword) |
| Branch patterns accepted as implementation PRs | `ai/issue-<n>`, `ai-implement-<n>` |

What this means for operators: a PR on a `claude/…` or feature branch that references an orchestrator issue with `Refs #<n>` survives that issue's stall recovery. PRs the orchestrator opened on `ai/issue-<n>`, and PRs whose body says `Closes #<n>`, are closed exactly as before.

### For contributors

`surface_reissue_closed_without_pr` still consults the broad, unfiltered linked-PR set on purpose: it only surfaces a warning and never blocks recovery, so it does not spend one request per candidate to apply the same filter. Its docstring records that choice. Two tests in `tests/test_orchestrate_poll_process.py` run the real bash functions against a stubbed `gh` and pin the close, skip, unknown-state, and one-request-per-candidate behaviours plus the predicate's accept and reject cases.
