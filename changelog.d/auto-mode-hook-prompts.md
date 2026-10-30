<!-- changelog: changed -->
- **Auto-mode sessions no longer stop at a permission prompt for an unstamped push or a `gh api` read with an unusual shell shape.** Two repo hooks used to ask a person even in Auto mode, which left sessions waiting minutes for a click.

When a Claude Code session runs in Auto mode, the pre-push test gate (`.claude/hooks/prepush_affected_tests.py`) now blocks a push that has no test stamp and tells the session to run `python3 .claude/hooks/prepush_affected_tests.py stamp` and push again, instead of prompting. The `gh api` guard (`.claude/hooks/gh_api_write_guard.py`, CLAUDE.md §23.H) now leaves commands it cannot read safely, such as a `for` loop with an unquoted `$i`, `$'\t'` quoting, or `gh api` inside `python3`, to Auto mode's own safety check instead of prompting. This applies only when no allow rule could match the command: Auto mode approves a command that matches an allow rule without running its safety check, so commands containing `gh api repos/` or `gh api user`, and commands any other `Bash(...)` allow rule in the project or user settings could match (such as `Bash(test *)` around a hidden `$(gh api -X DELETE ...)`, also behind a wrapper like `timeout 60`), are refused with steps to rewrite them instead (see the entry for #8180). The guard reads allow rules from the project, user, `CLAUDE_CONFIG_DIR` and managed settings files, and it also refuses, with the same steps, a command where brace or `$'...'` quoting sits next to an `@`, which could turn into a field that uploads a local file. Prompts stay where a person has to decide: a stamp that records test failures, bulk and tag pushes, and any `gh api` call the guard reads as a write (closing, merging, DELETE, another repository, a file-backed field). Every other permission mode behaves exactly as before.

| The numbers that matter | Value |
| --- | --- |
| Hooks changed | 2 (`prepush_affected_tests.py`, `gh_api_write_guard.py`) |
| Modes affected | Auto only (`permission_mode` == `auto`) |
| Headless pipeline modes | unchanged: `dontAsk`, `bypassPermissions` |

What this means for operators: in Auto mode, expect pushes to wait while the session runs the affected tests, and expect far fewer prompts. Consumer repos pick this up on the next `.claude/` sync.
