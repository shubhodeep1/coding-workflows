<!-- changelog: fixed -->
- **The merged-PR guard now distinguishes numeric push refspecs from file-descriptor redirects.** It checks the named branch when a numeric refspec precedes a spaced, quoted, escaped, or `&>` output redirect, while ignoring an adjacent `2>` or `2>&1` redirect as a file descriptor.

Commands such as `git push origin 2&>out` retain the merged-branch check for `2` rather than checking the current branch. The guard ignores the descriptor in `git push origin HEAD:feature/open 2>&1` and prompts for confirmation when it cannot resolve a push destination. The source and consumer-template hook copies stay identical.

What this means for operators: redirecting push output no longer bypasses a merged-branch check or creates a spurious numeric destination check.
