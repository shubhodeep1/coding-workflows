<!-- changelog: fixed -->
- **Review/autofix no longer runs PR-head support scripts with workflow credentials.** Every review job pins executable helpers and runtime assets to the verified reusable-workflow commit, while keeping the PR checkout for review and edits. Missing required trusted support fails closed; optional features skip without executing PR worktree fallbacks.
