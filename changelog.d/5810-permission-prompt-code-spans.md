<!-- changelog: security -->
- **A logged command can no longer inject Markdown into the `ai:permission-prompt` issues and comments that the Claude issue implementer reads as spec.** The command shape, the tool name, and each prompt reason now render in a code span that their own backticks cannot close.

`.claude/scripts/permission_prompts.py` used to write the command shape into a one-backtick code span on the `**Pattern:**` line of each issue body and of the class "Seen again" comment, and printed prompt reasons as plain Markdown. A command word containing a backtick and a newline could close that span and add bold text, links, or forged instructions to a comment posted under the owner's account. `/implement-issue-claude` counts every owner comment as spec, so it would read that text as part of its task (security finding #5810, `A03:2021-Injection`, medium). Each of these values is now written as one line with `<!--` escaped, inside a backtick run one longer than any run in the value. `/implement-issue-claude` step 1 also says that this generated text is evidence and never spec, whatever account posted it.

| The numbers that matter | Value |
| --- | --- |
| Rendering paths guarded | 3 (`issue_body`, `comment_body`, `class_comment_body`) |
| Values guarded per path | shape, tool name, every prompt reason |
| Signature changes for filed issues | 0 (only the rendering changes) |
| Extra API calls | 0 |

What this means for operators: new `ai:permission-prompt` issues and comments show the shape, the tool name, and the reasons literally, backticks included. Issues already filed are not rewritten. A shape without backticks renders exactly as before.

### For contributors

The fix lands in the `workflow-templates/.claude/` twins of `scripts/permission_prompts.py` and `commands/implement-issue-claude.md` (interim twin-first rule, #4785). `.claude/` follows at the twin sync. The new helper is `_markdown_code_span`. `_pattern_line` and `_reason_line` keep their return values. `tests/test_permission_prompt_duplicates.py` pins the code spans, the padding for values that start or end with a backtick, and the step 1 rule.
