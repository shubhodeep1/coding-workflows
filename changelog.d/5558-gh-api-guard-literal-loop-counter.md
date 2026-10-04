<!-- changelog: fixed -->
- **The `gh api` permission guard approves literal-ID read loops again in interactive sessions.** Since #6127, a loop such as `for r in 1 2; do gh api repos/o/r/actions/runs/$r/jobs; done` prompted for permission, because the unquoted-expansion check also caught the loop counter.

#6176 and #6175 fixed the consumer copy, `workflow-templates/.claude/hooks/gh_api_write_guard.py`. An unquoted `gh api` expansion now asks unless the whole command passes the read-loop validator, whose body may hold only classified reads and `echo`. This change copies that fix to `.claude/hooks/gh_api_write_guard.py`, the hook this repository's own sessions run, so the two copies match again. Loops that reassign the counter (`r=`, `read`, `. file`, `r[0]=`, `select`, `trap`), any other expansion, and shell-rewrite hazards inside a loop still prompt. 22 new test cases pin those cases.

What this means for operators: sessions in coding-workflows stop pausing on the read loops that CLAUDE.md §23.H allows, and the `gh api permission guard hook tests` step in `tests-hooks-and-orchestrator` passes on `main` again.
