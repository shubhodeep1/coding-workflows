<!-- changelog: security -->
- **The merged-PR guard now checks numeric push refspecs before `&>` and `&>>` redirects.** A redirected `git push` to a branch whose PR already merged is no longer overlooked.

When a numeric refspec touched an ampersand redirect, the guard mistook it for a file descriptor and skipped the destination branch check, even though Bash passes the number to git. The interactive-session hook now retains that refspec and checks the target branch's PR history as it does for other pushes. Numeric file descriptors on regular redirects, such as `2>&1`, are still excluded from the arguments being checked. The same protection reaches consumer repos through the mirrored hook on the next stable sync.

What this means for operators: redirecting a push's output cannot bypass the merged-PR branch guard for numeric branch names.
