<!-- changelog: changed -->
- **Claude Code sessions now edit files with the Edit and Write tools, never an inline interpreter script, and never work around a blocked `.claude/` edit.** CLAUDE.md §23.I gains both rules and names the repository root's `.claude/**` as the only protected path.

On 2026-09-27 an unattended `/implement-plan-claude` stage session rewrote two `workflow-templates/.claude/commands/` files with `python3 - <<'PYEOF' … PYEOF`. Claude Code cannot parse an inline script, so no `permissions.allow` rule can approve it, and the stage stopped at a permission prompt that `.claude/scripts/permission_prompts.py` filed as issue #4678. CLAUDE.md §23.I now says that repository files, including the byte-identical `workflow-templates/` twins, are edited with the Edit and Write tools. It also rules out `python3 - <<'EOF'`, `python3 -c`, `node -e`, `perl -e`, and heredocs fed to a shell. The same section's triage clause now says that `workflow-templates/.claude/**` is not a protected path. An `ai:permission-prompt` issue about those files is therefore fixed rather than closed as not planned. A second rule covers the protected path itself. Once an Edit or Write to the repository root's `.claude/**` is blocked or denied, a session never retries that write through Bash, `python3`, `sed`, `tee`, `cp`, a heredoc, or any other tool. It stops at `Status: BLOCKED`, names each file and its exact edit, and the operator's watched session applies it.

| The numbers that matter | Value |
| --- | --- |
| Prompt that triggered this | issue #4678, pattern `python3 * << * ; git diff --stat -- *` |
| Protected path for the §23.I triage | the repository root's `.claude/**` only |
| Repos affected | this repo, and every consumer on its next `@stable` sync of `CLAUDE.md` |

What this means for operators: one way for an unattended stage session to stop at a prompt is gone. Editing a file inside the repository no longer goes through a shell script that nothing can approve. A blocked edit to the root `.claude/` reaches you as a `Status: BLOCKED` stop that lists each file and edit to apply, not as a script that writes around the block. Both rules are guidance, not a hook. A session that still reaches for an inline script gets reported by the existing prompt report, filed as a comment on the matching `ai:permission-prompt` issue.

### For contributors

`tests/test_permission_prompts.py::test_claude_md_routes_file_edits_through_edit_tools` and `::test_claude_md_never_retries_a_blocked_claude_dir_edit` pin the new CLAUDE.md §23.I text. No `.claude/**` file, allow rule, or helper changed.
