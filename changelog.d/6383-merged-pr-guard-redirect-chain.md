<!-- changelog: fixed -->
- **The merged-PR guard preserves numeric push targets across chained redirects.** A skipped redirect target can no longer cause the guard to drop a numeric branch refspec and check the current branch instead.

What this means for operators: pushes such as `git push origin 2 2>&2>/dev/null` now check branch `2` for an already-merged PR before proceeding.
