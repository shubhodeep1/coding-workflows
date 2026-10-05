<!-- changelog: fixed -->
- **The merged-PR guard checks numeric branch refspecs even when prefixed with empty quotes.** It only treats a whole, unquoted numeric word next to a redirect as a file descriptor, so quoted or escaped numeric branch names cannot bypass the merged-PR check.

The live hook and consumer template use the same word-boundary test; ordinary numeric file-descriptor redirects still work as before.

The guard also preserves a numeric push target across chained redirects instead of mistaking it for a file descriptor.

What this means for operators: pushes such as `git push origin ''2>/dev/null` and `git push origin 2 2>&2>/dev/null` check branch `2` before proceeding instead of silently checking only the current branch.
