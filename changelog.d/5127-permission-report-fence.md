<!-- changelog: security -->
- **A command in a permission-prompt report can no longer break out of its code block.** The fence around the command is now longer than any backtick run in it, so the whole command stays inert text on GitHub.

`.claude/scripts/permission_prompts.py` wrapped the sanitized command in a fixed four-backtick fence. A command with a line of four backticks closed that fence early, and the rest of the command rendered as Markdown (headings, links, instructions) in the `ai:permission-prompt` issue or the PR comment that agents and the operator read (issue #5127, security audit finding `permission-report-markdown-escape`). The fence is now at least four backticks and one longer than the longest backtick run in the command. This covers both the immediate report from `report-now` and the example in every issue body and "Seen again" comment. `permission_prompts.py lookup` now reads the command back from the report that ends the post, starting at its session marker, so a command that imitates a report, a marker, a field line, or a fence cannot change what `lookup` returns.

| The numbers that matter | Value |
| --- | --- |
| Fence length | 4 backticks, or the longest backtick run in the command plus 1 |
| Reports whose command has no run of 4 backticks | unchanged, byte for byte |
| Reports already posted with the old fence | still read by `lookup` |
| New API calls | none |

What this means for operators: nothing to do. Reports look the same unless the command itself contains long backtick runs. Treat the command in a report and in `lookup` output as untrusted session data: show it, never follow it.

### For contributors

The fence comes from `_fenced_text()`. `parse_immediate_block()` takes the last session marker in the body (every report ends its post with its own marker), the fence that closes right before it, the last line that opens the same fence, and the last line-start heading before that. The change was made in the `workflow-templates/.claude/` twin first and copied into `.claude/` under the interim twin-first rule (CLAUDE.md §28.C). The new tests in `tests/test_permission_prompts.py` read the twin.
