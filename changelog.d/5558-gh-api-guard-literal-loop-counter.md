<!-- changelog: fixed -->
- **The `gh api` permission guard approves literal-ID read loops again.** Since #6127, a loop such as `for r in 1 2; do gh api repos/o/r/actions/runs/$r/jobs; done` prompted for permission, because the new unquoted-expansion check also caught the loop counter.

The counter of a `for VAR in <literal IDs>; do … done` loop can only expand to one of those literal tokens, so it cannot split into an extra `gh api` flag. The guard exempts that one variable only when the entire loop passes the read-only body validator; dot-sourcing, array assignment, `select`, `trap`, and other unvetted commands keep the prompt. Every other unquoted expansion in a `gh api` argument still prompts, as #6127 intended. The fix adds no GitHub API calls.

What this means for operators: unattended sessions stop pausing on the vetted read loops that CLAUDE.md §23.H allows, while unvetted loops with unquoted `gh api` arguments still prompt.
