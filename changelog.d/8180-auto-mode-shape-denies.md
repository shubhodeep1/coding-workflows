<!-- changelog: changed -->
- **Auto-mode sessions no longer stop on prompts about a command's shape.** The pre-push test gate, the merged-PR guard and the `gh api` guard now refuse those commands with instructions to rewrite them instead of asking a person.

On 2026-10-26 the activation sessions opened by the daily routine stopped on more than a dozen prompts. The cases were a branch created and pushed in one command, `gh api` loops with `$id` paths and `> $D/file` redirects, and a `python3` heredoc whose text mentioned `gh api`. In Auto mode a hook prompt still waits for a human. These cases now get a deny that runs nothing and tells the session what to do: commit first, push with a plain `git push -u origin <branch>`, make one `gh api` call per ID, and use the Edit or Write tool for text. Classified `gh api` writes, branch deletions, default-branch pushes, bulk pushes and pushes to other repositories still prompt. Outside Auto mode nothing changes.

| The numbers that matter | Value |
| --- | --- |
| Hooks changed | `.claude/hooks/prepush_affected_tests.py`, `.claude/hooks/pr_merge_status_guard.py`, `.claude/hooks/gh_api_write_guard.py` |
| Docs updated | CLAUDE.md §21.E, §23.D, §23.H |

What this means for operators: unattended Auto-mode sessions fix these commands themselves instead of waiting for you. Consumer repos get the change on the next `@stable` sync.

### For contributors

coding-workflows' own `.claude/settings.json` now also allows `mcp__claude-code-remote__create_session`, so the operator's daily activation routine can open sessions without the Auto-mode check. The template copy does not, and `.github/ai/claude_template_divergence.json` lists `settings.json`. Any other settings change must be made in both copies by hand. `settings_match_template()` in `tests/claude_template_drift.py` allows exactly that one extra rule, and `tests/test_no_session_automation.py` exempts exactly that one line.
