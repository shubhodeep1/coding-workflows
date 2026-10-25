<!-- changelog: changed -->
- **Auto-mode sessions no longer stop at a permission prompt for an unstamped push or a `gh api` read with an unusual shell shape.** Two repo hooks used to ask a person even in Auto mode, which left sessions waiting minutes for a click.

When a Claude Code session runs in Auto mode, the pre-push test gate (`.claude/hooks/prepush_affected_tests.py`) now blocks a push that has no test stamp and tells the session to run `python3 .claude/hooks/prepush_affected_tests.py stamp` and push again, instead of prompting. The `gh api` guard (`.claude/hooks/gh_api_write_guard.py`, CLAUDE.md §23.H) now leaves commands it cannot read safely, such as a `for` loop with an unquoted `$i`, `$'\t'` quoting, or `gh api` inside `python3`, to Auto mode's own safety check instead of prompting. This applies only when the command does not contain `gh api repos/` or `gh api user`: Auto mode approves those through the repo's `Bash(gh api repos/*)` and `Bash(gh api user *)` allow rules without running its safety check, so they still prompt. Prompts stay where a person has to decide: a stamp that records test failures, bulk and tag pushes, and any `gh api` call the guard reads as a write (closing, merging, DELETE, another repository, a file-backed field). Every other permission mode behaves exactly as before.

| The numbers that matter | Value |
| --- | --- |
| Hooks changed | 2 (`prepush_affected_tests.py`, `gh_api_write_guard.py`) |
| Modes affected | Auto only (`permission_mode` == `auto`) |
| Headless pipeline modes | unchanged: `dontAsk`, `bypassPermissions` |

What this means for operators: in Auto mode, expect pushes to wait while the session runs the affected tests, and expect far fewer prompts. Consumer repos pick this up on the next `.claude/` sync.
