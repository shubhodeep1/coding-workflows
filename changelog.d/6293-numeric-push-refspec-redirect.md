<!-- changelog: fixed -->
- **The merged-PR push guard now checks numeric branch names before output redirects.**

Commands such as `git push origin 123 > /dev/null` now check branch `123` rather than the session's current branch. Attached file-descriptor redirects such as `2>/dev/null` are still treated as redirects. If an explicit push destination cannot be determined, the guard asks for confirmation instead of checking an unrelated branch. When a push includes several unknown targets, it emits one confirmation only after checking known targets for merged PRs. With `--repo` and a positional remote, it checks the refspecs after that remote instead of treating the remote name as a branch.

What this means for operators: numeric branch pushes cannot bypass the merged-PR check through a separate redirect, and uncertain destinations require approval before pushing.
