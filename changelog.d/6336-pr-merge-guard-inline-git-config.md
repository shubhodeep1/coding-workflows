<!-- changelog: security -->
- **The merged-PR guard now checks the effective destination of pushes with inline Git configuration.** A per-command remote URL or push mode can change which branch `git push` updates; the guard now applies those options when checking PR history and blocks configurations it cannot safely resolve.

Both the live hook and the consumer template reject unresolvable inline configuration instead of letting a push bypass the merged-branch check. Normal pushes keep their existing behavior. Consumer repos receive the protection through the next stable workflow sync; no configuration is required.
