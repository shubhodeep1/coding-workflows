<!-- changelog: changed -->
- **Automation sessions now lead with their issue and PR numbers.** New Claude sessions are titled `#<issue> · PR #<pr> — <title>`, using whichever parts exist, so the claude.ai sidebar shows which issue and PR a session belongs to even when it truncates the end.

`/implement-plan-claude` creates stage, checker, and `/deploy-activate` sessions with the prefix. A stage renames itself when it opens its PR. The Claude issue pickup and `/claude-issue-dispatch` start issue sessions as `#<N> · issue <repo>#<N> — implement`, and a fresh `/fix-claude-pr` session adds `#<I> · ` when its head branch is `claude/implement-plan-issue-<I>-…`. The checker reuse check and the zombie-checker cleanup now match any title that *contains* `implement-plan <slug> — checker`, so checkers created before this change, or renamed by hand, are still found. `PR #<n> status check-in` titles, which CLAUDE.md §26.B step 1b matches exactly, and all Routine names are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Example stage title | `#4723 · PR #4729 — implement-plan issue-4723-require-plan-run-id — conformance 1/3` |
| Titles left unchanged | `PR #<n> status check-in`, and the checker's `PR #<n> <merged \| closed> — handed to …` renames |
| New test file | `tests/test_session_titles.py` (runs in the `/implement-plan-claude command contract tests` step of `ci.yml`) |

What this means for operators: you can match a session to its issue and PR from the first characters of its title, without opening it. Sessions started before this change keep their titles until someone renames them.

### For contributors

The rule lives in the `### Session titles` subsection of `.claude/commands/implement-plan-claude.md`. The PR part names the PR the session works on, or else the project's final PR. A rename strips an existing `#<n> · ` and `PR #<m> — ` prefix first, so prefixes never stack. The checker copies one of three titles the arming stage fills into its prompt (success, review round, block), and never builds a title itself.
