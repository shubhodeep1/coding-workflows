<!-- changelog: security -->
- **The `gh api` permission guard now prompts for every file-backed `-F` value and every `--input`, so a routine comment or a read can no longer publish a local file without a prompt.**

`.claude/hooks/gh_api_write_guard.py` (CLAUDE.md §23.H) checked only the field names of a routine write, never the values. `gh api repos/<repo>/issues/1/comments -F body=@/path/to/credential` was approved with no prompt, and `gh` read the file and posted its contents as a comment. The same file read also reached GitHub through calls the guard treated as reads: a GET or HEAD with `-F q=@<file>` in the query string, a GraphQL variable `-F v=@<file>`, and a GET with `--input <file>`. The guard now classifies any call with an `-F`/`--field` value that starts with `@` (a file, or `@-` for stdin), or with `--input`, as a write, whatever the method, endpoint, or repository. The prompt names the file-backed field. `-f`/`--raw-field` values are sent literally and read no file, so `-f body=@octocat` is still allowed.

| The numbers that matter | Value |
| --- | --- |
| Finding | `api-guard-allows-file-backed-comment`, high, issue #4619 |
| New guard test cases | 29 (17 new file-backed or `--input` asks, 7 classification reasons, 1 ask reason, 1 hook process, 3 raw-field allows) |
| Existing fixtures now expected to ask | 5 observed `-F body=@...` commands |
| GitHub API calls added | 0 |

What this means for operators: a session that edits a PR body or a progress comment with `-F body=@<file>` or `--input <file>` now stops at a permission prompt. Post and edit bodies with the GitHub MCP tools (`mcp__github__update_pull_request`, `mcp__github__add_issue_comment`, `mcp__github__update_issue_comment`) or pass the text inline with `-f body=...`. The fix reaches consumer repos on the next `@stable` sync of `.claude/`.

### For contributors

`classify()` checks field values and `--input` before the GraphQL and read-method branches, so the old `--input` check after the read-method branch is gone. The GraphQL branch's `query=@` check stays as defence in depth. No identifier changed.
