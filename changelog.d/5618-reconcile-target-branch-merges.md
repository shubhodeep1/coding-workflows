<!-- changelog: security -->
- **The orchestrator poller no longer marks an issue merged because an unrelated PR saying `Fixes #<n>` merged into the wrong branch.** Every poller path that turns a merged linked PR into merged state now applies the target-branch and identity rule `close_merged_issues_sweep` already enforced.

Before this change, four paths in `scripts/orchestrate_poll_process.sh` accepted any merged PR carrying a closing keyword for the issue, whatever branch it merged into and whoever pushed it. Those paths were the current-wave label reconciliation, the validation-dispatch wave gate, stall recovery's `ai:merged` tag, and the backward scan's `ai:ready-to-merge` promotion. Such a PR could force `ai:merged` on a wave child and mark it `merged` in `check-wave-status`, which could advance its wave with the child's work never done. Now a merged PR counts only when it landed in this repository, on the default branch or on the issue's target branch from a same-repository automation head for that issue. A PR in another repository that closes the issue by reference never counts, even when it merged into a branch with the same name. The target branch is the project's integration branch, or the issue's `Integration branch:` line for stall recovery. Every other merge is logged and ignored.

| The numbers that matter | Value |
| --- | --- |
| Paths gated | 4 (reconcile loop, validation-dispatch gate, stall-recovery tag, backward-scan promotion) |
| New log keys and values | `STALL_MERGED_LABEL_REJECTED`; `LINKED_PR_CROSS_REF_REJECTED … reason=non_target_base \| unverified_identity \| foreign_base_repo` |
| Extra API calls | none on the reconcile loop, backward scan, or wave gate; 2 REST reads per merged-PR hit in stall recovery |
| Source issue | #5618 (security cycle 3 of the #4813 project) |

What this means for operators: a wave only advances on work that merged where the project expects it. When a legitimate merge is rejected, `LINKED_PR_CROSS_REF_REJECTED` or `STALL_MERGED_LABEL_REJECTED` in the poller log says why. Usually the PR was hand-made into a project branch rather than pushed from an `ai/issue-<n>` branch. The issue then resolves through its labels, the close sweep, or `issue_pr_status.yml`, which already apply the same rule.

### For contributors

The shared predicate is `_pr_json_merged_into_issue_target <issue> <pulls/<n> JSON> [<target branch> …]` in `scripts/orchestrate_poll_process.sh`. Callers check `_pr_json_is_issue_implementation_pr` first. `_fetch_candidate_issue_details_graphql` now returns `head_repo`, `base_repo`, and `default_branch` on `linked_pr`. `tests/test_linked_pr_implementation_guard.py` covers the predicate, the stall-recovery tag, the validation-dispatch wave gate through the real `check-wave-status`, and the call-site wiring.
