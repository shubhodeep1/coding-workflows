<!-- changelog: fixed -->
- **The merged-PR guard now treats redirected `cd` and `exit` commands as uncertain when their redirects may fail.**

Pushes after an uncertain directory change are checked against the session checkout rather than an assumed working directory. If that checkout is on a merged PR, the guard blocks the push; otherwise it requests confirmation before the push proceeds. Redirects to literal `/dev/null` retain their existing behavior, as do warning-only checks for uncertain commits. Consumer repos receive the updated guard through the template sync.

What this means for operators: a failed shell redirect cannot silently bypass the merged-PR push check.
