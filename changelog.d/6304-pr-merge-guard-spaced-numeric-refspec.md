<!-- changelog: security -->
- **The merged-PR guard checks numeric push branches even before output redirects.** A spaced or quoted number in `git push origin 2 > /dev/null` remains a branch argument rather than being discarded as a file descriptor; adjacent unquoted `2>/dev/null` remains a redirect. The live hook and consumer template stay in sync.
