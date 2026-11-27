<!-- changelog: changed -->
- **The merge-train tick now logs the merge-lease queue.** After each lease base's decision, a `MERGE_LEASE_QUEUE base=<ref> holder=<n|-> held_s=<s|-> waiters=<count> order=<comma list|->` line lists the PRs waiting for the lease in queue order, so the time a PR waits (its first to its last appearance in `order=`) can be measured. The tick's `MERGE_LEASE ... outcome=released` lines now end with `held_s=<s|-> merged=true|false`, and its `outcome=granted` lines gain `queue_len=<eligible waiters>` (before `holders=<n>` when several PRs hold the lease). The earlier fields of these lines are unchanged.

This is observability only: no lease decision, record, label or GitHub API call changes. When the staged `pr_checks_lib.sh` is older than this change, the queue line and `queue_len=` are left out.

What this means for consumer repos: after the next `@stable` sync, merge-train logs gain `MERGE_LEASE_QUEUE` lines and the extra fields. Nothing needs to be done.

Refs #9781
