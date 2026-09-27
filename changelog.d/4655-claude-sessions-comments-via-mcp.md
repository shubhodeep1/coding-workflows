<!-- changelog: changed -->
- **Claude fixer and `/implement-plan-claude` sessions post PR and issue comments through the GitHub MCP tools.** Hand-built `gh api … --input <file>` or heredoc JSON bodies always stop at a permission prompt, which stalls a session nobody is watching.

CLAUDE.md §23.D item 4 and the Rules of `.claude/commands/fix-claude-pr.md` and `.claude/commands/implement-plan-claude.md` now require `mcp__github__add_issue_comment` / `mcp__github__update_issue_comment` for comments, and forbid `gh api … --input <file>`, `-F body=@<file>`, and heredocs that build a JSON body. The prompt that motivated this came from a fixer session on PR #4601 that built its finding-by-finding reply with a Python heredoc piped into `gh api --input`.

What this means for operators: fewer unattended Claude sessions stopping at a permission prompt; nothing to configure.
