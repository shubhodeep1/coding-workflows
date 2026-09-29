<!-- changelog: added -->
- **Inline-interpreter file edits are now denied at once, with a redirect to the Edit and Write tools.** A new `PreToolUse` hook, `.claude/hooks/inline_edit_guard.py`, stops unattended sessions from stalling at a permission prompt nobody answers.

On 2026-09-28 the operator answered at least 8 prompts by hand for commands such as `python3 - <<'EOF' … write_text(…)`, although none of the target files was a protected path. The hook sits next to the `gh api` guard under the `Bash` matcher in `.claude/settings.json`. It answers `permissionDecision: deny` for `python` / `python3` programs from `-c` or a stdin heredoc that write files, and for `sed -i`, `perl -i` / `-pi`, `ruby -i`, and `awk -i inplace`. The deny reason tells the session to use the Edit tool (exact `old_string` / `new_string`) or the Write tool, so it retries in the same turn with no human involved. Read-only interpreter use, `pytest`, `python3 -m …`, scripts run from a file path, and interpreter text inside `git commit -m`, `echo`, or a quoted string get no decision.

| The numbers that matter | Value |
| --- | --- |
| Decision | `deny` (never `ask`) |
| Kill switch | `CLAUDE_INLINE_EDIT_GUARD=off` (default on) |
| Log key | `INLINE_EDIT_GUARD action=deny kind=<python\|sed\|perl\|ruby\|awk> session=<id>` |
| Failure mode | fails open: bad payload, unparseable command, or internal error gives no decision |
| API calls | 0 |

What this means for operators: sessions that reach for a `python3` heredoc or `sed -i` to change a file switch to the Edit tool on their own instead of waiting for you. Each deny is recorded in the permission-prompt log with `source: "inline_edit_guard"`, and `.claude/scripts/permission_prompts.py` reports those as `expected_denies` without filing `ai:permission-prompt` issues. Consumer repos receive the hook and its `settings.json` wiring on the next `@stable` sync.

### For contributors

The hook reuses `gh_api_write_guard.py`'s quote-aware tokenizer, loaded from the sibling file by path, and records denies through `permission_prompt_logger.py`'s own functions. CLAUDE.md §23.I documents it. `tests/test_inline_edit_guard.py` runs in its own `ci.yml` step, and `tests/test_permission_prompts.py` covers the filing exclusion.
