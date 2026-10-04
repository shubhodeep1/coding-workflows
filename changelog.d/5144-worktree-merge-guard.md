<!-- changelog: security -->
- **The merged-PR guard now checks the repository and destination branch of each guarded git command.** A detached worktree can push to an open PR branch without being blocked by an unrelated merged PR in the session checkout.

The Bash PreToolUse hook follows resolvable `cd`, `git -C`, and git-directory overrides when checking commits and pushes. Explicit push refspecs are checked against their destination branch and source commit, so a push to a merged branch cannot inherit a safe verdict from the checkout's branch. When the directory or refspec cannot be resolved, the hook warns and uses the session checkout check instead. Repeated destinations share one PR lookup per repository and branch within the command.

What this means for contributors: work from scratch worktrees without detaching the main checkout to bypass a false merged-PR block.
