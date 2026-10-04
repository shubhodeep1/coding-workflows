<!-- changelog: fixed -->
- **The `gh api` permission guard approves literal-ID read loops again.** Since #6127, a loop such as `for r in 1 2; do gh api repos/o/r/actions/runs/$r/jobs; done` prompted for permission, because the new unquoted-expansion check also caught the loop counter.

The counter of a `for VAR in <literal IDs>; do … done` loop can only expand to one of those literal tokens, so it cannot split into an extra `gh api` flag. The guard now exempts that one variable, and only while nothing in the loop body can reassign it (`VAR=`, `VAR+=`, `read`, `declare`, `printf -v`, `eval`, a nested `for`, and similar). Every other unquoted expansion in a `gh api` argument still prompts, including calls following the loop. The active consumer template carries this check; the source repository's retired `.claude` tree is not restored. The fix adds no GitHub API calls.

What this means for operators: unattended sessions stop pausing on the read loops that CLAUDE.md §23.H allows, and the `tests-hooks-and-orchestrator` CI job is green again on `main` and on every branch cut from it.
