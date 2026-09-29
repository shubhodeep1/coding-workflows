<!-- changelog: fixed -->
- **The `gh api` permission guard no longer prompts on `gh api` text inside single quotes, and now prompts on a `gh api` call hidden in unquoted backticks.**

`.claude/hooks/gh_api_write_guard.py` (CLAUDE.md §23.H) looked for command substitutions in each word after the shell's quotes were stripped. That caused two errors:

- A single-quoted `sed` expression, commit message or `echo` that merely mentioned a backticked `` `gh api user` `` was treated as a hidden call, so it stopped at a permission prompt. On 2026-09-28 this hit a `/deploy-activate` session updating its activation log.
- `` echo `gh api -X DELETE …` `` with unquoted backticks got no decision from the guard at all.

The guard now reads the raw command the way Bash does. Backtick substitutions outside single quotes, and `$(...)` inside double quotes, count as hidden calls when their body holds `gh api`. Single-quoted text is data.

| The numbers that matter | Value |
| --- | --- |
| New guard test cases | 27 (8 new prompts, 5 new no-decision cases, 14 quoting cases for `substitution_bodies`) |
| GitHub API calls added | 0 |

What this means for operators: fewer unattended stops on log and commit edits that quote a `gh api` command, and no silent pass for a backticked `gh api` write. The fix reaches consumer repos on the next `@stable` sync of `.claude/`.

### For contributors

`has_hidden_gh_api` takes the command as a new optional third argument. Without it, it keeps the older token-level reading, so existing callers are unaffected. An unquoted `$(...)` is still split into its own segments and classified directly, as before.
