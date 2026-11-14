<!-- changelog: fixed -->
- **Standalone stall recovery no longer loops on `ai:ready-to-merge` issues whose PR already merged into an integration branch.** The poller now notices the merge, skips recovery and logs `STALL_SKIP ... reason=linked_pr_merged`.

A PR merged into `orchestrator/project-*` never sets GitHub's `willCloseTarget`, so neither GraphQL linked-PR lookup found it. For `ai:ready-to-merge` the poller then ran `attempt_merge` every round with no open PR to merge and, from the judge trigger count onward, called the stall judge. The judge saw `linked_pr.state: "closed"` with no merge signal, asked for a human, and that `escalate_human` was normalised back to `attempt_merge`, so the loop never ended. Now:

- `ai:ready-to-merge` uses the same implementation-PR resolver fallback as `ai:done`. When the resolved PR has merged, the existing merged guard skips recovery without touching the stall counters, prints `STALL_SKIP issue=<n> reason=linked_pr_merged pr=<pr> phase=<phase> action=<action>`, and hands the issue to the existing merged-PR reconcile. `attempt_merge` reuses the resolved PR, so the tick makes no extra API call.
- The stall judge's `linked_pr` diagnostics gain `merged` and `merged_at`, read from the PR payload it already fetches, and `state` reads `merged` for a merged PR.
- A judge `escalate_human` is kept, not normalised to `attempt_merge`, when the linked PR is known to be closed or merged. The poller logs `Stall judge action 'escalate_human' kept for issue #<n>: linked PR #<pr> is <state> ...`.

Refs #9482.
