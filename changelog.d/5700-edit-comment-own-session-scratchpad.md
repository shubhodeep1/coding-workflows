<!-- changelog: security -->
- **`.claude/scripts/edit_comment.py` now reads `--body-file` and `--replacements` only from the calling session's own scratchpad, not any session's.** A file in another Claude Code session's scratchpad, or in another user's, can no longer be published as a comment through the allowlisted helper.

The #5452 restriction accepted any path shaped like `<temp dir>/claude-*/<project>/<session>/scratchpad/<file>`. On a shared temp filesystem, a prompted unattended agent could therefore pass a readable file from another session's scratchpad and publish it with no permission prompt (#5700, `A01:2021-Broken Access Control`, found by `security-audit.yml`). The helper now also requires the first component to be `claude-<uid>` for the calling process's uid and the `<session>` component to equal `CLAUDE_CODE_SESSION_ID`, which Claude Code exports to every Bash command. It also requires the file to be owned by that uid, and opens the checked path one directory at a time without following symlinks, so a directory swapped for a symlink after the check makes the open fail instead of reading an outside file. When `CLAUDE_CODE_SESSION_ID` is unset, empty, or not a single path component, the caller's scratchpad cannot be verified and every file is refused. A refused path exits 1 with a JSON `error` before the comment is read, and the file's content is never printed.

| The numbers that matter | Value |
| --- | --- |
| Accepted location | `<temp dir>/claude-<os.getuid()>/<project>/$CLAUDE_CODE_SESSION_ID/scratchpad/<file>` |
| File owner | the calling uid (`fstat` on the open descriptor) |
| `CLAUDE_CODE_SESSION_ID` missing or malformed | every file refused, exit 1 |
| New API calls | 0 |

What this means for operators and consumer repos: nothing to configure. Stage sessions that write their replacements file into their own scratchpad with the Write tool keep working. A caller that passes another session's file, or runs without `CLAUDE_CODE_SESSION_ID` (an older Claude Code), gets exit 1 and should write the file into its own scratchpad or rewrite the comment with `mcp__github__update_issue_comment`. The change reaches consumer repos with the next `.claude/` sync.

### For contributors

The check is `_caller_scratchpad_identity` and `is_own_scratchpad_path` in `edit_comment.py`, called from `read_input_file` before the open; `_open_scratchpad_file` then opens each directory relative to its parent with `O_NOFOLLOW | O_DIRECTORY` and refuses every file on a platform without `dir_fd` support. `is_scratchpad_path` keeps its #5452 layout rules and signature. A session id must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, so `..` or a separator can never pick another directory. There is still no environment-variable override. `tests/test_edit_comment.py` covers another session's and another uid's scratchpad, a file owned by another uid, a parent directory swapped for a symlink after the check, missing and malformed session ids, and the new matcher.
