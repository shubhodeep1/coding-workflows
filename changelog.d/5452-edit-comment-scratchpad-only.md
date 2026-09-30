<!-- changelog: security -->
- **`.claude/scripts/edit_comment.py` now reads `--body-file` and `--replacements` only from the Claude Code session scratchpad.** Before this change, one allowlisted command could publish any local file, a credential file included, as an issue or PR comment with no permission prompt.

`.claude/settings.json` allowlists the helper, so its calls never prompt and never reach the `gh api` guard. `--body-file` read any path and PATCHed its text in as the comment body, so `edit_comment.py --body-file ~/.config/gh/hosts.yml` published the gh credentials (#5452, found during the #4619 audit). The helper now resolves the path first, following symlinks. It reads the file only when the result is a regular file with one hard link and at most 1,048,576 bytes under `<temp dir>/claude-*/<project>/<session>/scratchpad/`, where the temp dir is `tempfile.gettempdir()` or `/tmp`. A lookalike such as `/tmp/claude-x/scratchpad/` does not count. Any other path exits 1 with a JSON `error` before the comment is read, and the file's content is never printed, `--dry-run` included. The documented flow is unchanged: `/implement-plan-claude` stages already write their replacements file into the scratchpad.

| The numbers that matter | Value |
| --- | --- |
| Accepted location | `<temp dir>/claude-*/<project>/<session>/scratchpad/<file>` (temp dir = `tempfile.gettempdir()` or `/tmp`) |
| Largest input file | 1,048,576 bytes (`MAX_INPUT_FILE_BYTES`, 16 × the 65,536-character comment limit), checked before the read |
| Rejected | anything else, symlink or `..` escapes, hard links, directories, FIFOs, missing files, larger files |
| Exit code on rejection | 1 (invalid argument), before any API call |
| API calls on success | unchanged: 1 read, 1 PATCH |

What this means for operators and consumer repos: nothing to configure. Sessions that already write the file into the scratchpad keep working. A caller that passes a file from anywhere else gets exit 1 and should move the file into the scratchpad or rewrite the comment with `mcp__github__update_issue_comment`. The change reaches consumer repos with the next `.claude/` sync.

### For contributors

The check is `read_input_file` in `edit_comment.py`, with `is_scratchpad_path` and `_temp_roots`. It opens the resolved path with `O_NOFOLLOW | O_NONBLOCK`, checks `fstat` on the open descriptor (type, link count, size), and reads at most `MAX_INPUT_FILE_BYTES + 1` bytes before decoding them as UTF-8, so a file that grows after the check is still never read past the limit. There is no environment-variable override. `tests/test_edit_comment.py` covers each rejected shape, the scratchpad flow, and the path matcher.
