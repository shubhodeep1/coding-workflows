<!-- changelog: security -->
- **Fork PRs can no longer hold same-repository review PRs in the merge train.** Closes the high-severity STRIDE denial-of-service finding `fork-pr-blocks-merge-train` (issue #6500).

The merge-train gate and scheduled release now count only older PRs whose head repository matches the base repository, ignoring case. A fork named `ai/issue-*` and a deleted fork with no head repository cannot block a same-repository PR, consume its older-PR examination budget, or trigger a file-list fetch. Authorized same-repository PRs retain lowest-number-first ordering and the existing overlap check. The release backstop dispatches review when only fork blockers remain.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | 0 (head repository comes from the existing open-PR listing) |
| Fork PRs examined as blockers | 0 |

### For contributors

The `MERGE_TRAIN_ENABLED` switch, `MERGE_TRAIN_MAX_OLDER_PRS` cap, and one-shot manual bypass continue to work as before. The train logs `MERGE_TRAIN_FOREIGN_HEAD_SKIPPED` with PR numbers only; it does not print the fork repository name.
