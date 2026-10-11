<!-- changelog: security -->
- **Closing a PR without merging now closes only the issue that PR was opened for.** A PR author can no longer close an unrelated issue by referencing it and then closing the PR.

Before this change, `issue_pr_status.yml` labelled `ai:closed` and closed every issue a closed, unmerged PR referenced, using `GH_PAT`, including issues named only in the PR title or body. It now does so only when all of these hold: the PR head is in this repository, the head branch is `ai/issue-<n>` for that issue, and the PR author is the `GH_PAT` account. Every other referenced issue keeps its labels, its open state and its lineage, and the run logs `Unmerged close of PR #<pr> left issue #<n> unchanged (reason=<token>)`.

| Reason token | Meaning |
| --- | --- |
| `foreign_head` | The PR head is a fork or another repository |
| `unbound_issue` | The head branch is not `ai/issue-<n>` for this issue |
| `identity_unavailable` | The `GH_PAT` login could not be read (fails closed) |
| `author_mismatch` | The PR was not opened by the `GH_PAT` account |

Merged PRs follow the same rules as before. What this means for operators: abandoned pipeline PRs still close their issue; an issue a human PR closed this way before now stays open, and the poller's stall and unblock sweeps pick it up.
