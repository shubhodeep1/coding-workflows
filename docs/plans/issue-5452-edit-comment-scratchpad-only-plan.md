# edit_comment.py: read `--body-file` and `--replacements` only from the session scratchpad

Source issue: shubhodeep1/coding-workflows#5452 (https://github.com/shubhodeep1/coding-workflows/issues/5452)
Base branch: main
Security pass: run

## Summary

`.claude/scripts/edit_comment.py` is allowlisted in `.claude/settings.json`, so it never prompts, and its `--body-file` flag reads any local path and PATCHes the text in as an issue or PR comment. One allowlisted command can therefore publish a credential file (`~/.config/gh/hosts.yml`, `/tmp/mcp-config-*.json`) with no prompt. This plan makes the helper read `--body-file` and `--replacements` only from regular, singly-linked files inside a Claude Code session scratchpad, checked after resolving symlinks and before any API call, so the documented flow keeps working and the exfiltration path closes.

## Context

- #5452, found during the conformance audit of #4619 (the `gh api` guard now prompts on file-backed `-F body=@<file>` values). This is the same class of path through a different command.
- `.claude/settings.json:60-61` (and the `workflow-templates/` twin) allow `Bash(python3 .claude/scripts/edit_comment.py *)` and `Bash(PYTHONDONTWRITEBYTECODE=1 python3 .claude/scripts/edit_comment.py *)`, so the call never reaches `.claude/hooks/gh_api_write_guard.py` or a prompt.
- `edit_comment.py:152-156` reads `--body-file` with `Path(args.body_file).read_text(...)`; `load_replacements` (`edit_comment.py:54-59`) reads `--replacements` the same way. Neither checks the path.
- The only documented callers write the file into the session scratchpad: `/implement-plan-claude` [Helpers](../../.claude/commands/implement-plan-claude.md#comment-helper) ("Write the replacements file … with the Write tool into your scratchpad") and CLAUDE.md §23.I. No command, workflow, or script passes a file from anywhere else (`grep -rn edit_comment`, 2026-09-30).
- Claude Code's scratchpad lives at `<tmp>/claude-<uid>/<project>/<session>/scratchpad/` (this session: `/tmp/claude-0/-home-user-coding-workflows/<session uuid>/scratchpad`). The rest of `/tmp` is not safe to publish: it holds `mcp-config-<session>.json`, `claude-code*.log`, and diagnostic logs.
- `.claude/scripts/edit_comment.py` and `workflow-templates/.claude/scripts/edit_comment.py` are byte-identical (checked 2026-09-30); `tests/test_edit_comment.py::test_template_parity` enforces it. `CLAUDE.md` is the same file as `workflow-templates/CLAUDE.md` (symlink).

## Goals

- G1: `--body-file` and `--replacements` are read only when the path resolves (after symlinks, `Path.resolve(strict=True)`) to a regular file with exactly one hard link, under `<temp root>/claude-*/…/scratchpad/`, where `<temp root>` is the resolved `tempfile.gettempdir()` or `/tmp`.
- G2: Any other path (outside the scratchpad, a symlink pointing out of it, `..` traversal, a hard link, a directory, a FIFO, a missing file) exits 1 with a JSON `error` that names the rule and the fix, prints none of the file's content, and makes no GitHub API call.
- G3: The documented `--replacements` flow and `--body-file` from the scratchpad work unchanged (same output, exit codes, one read and one PATCH).
- G4: The root and `workflow-templates/` copies of the script stay byte-identical, and CLAUDE.md §23.I, `agents.md`, and the `/implement-plan-claude` Helpers section describe the restriction.

## Non-goals

- Removing `--body-file` or `--replacements` (§6: a removed CLI flag is a breaking change; AD-3).
- Changing the allow rules in `.claude/settings.json` or the `gh api` guard.
- Allowing the git working tree (AD-1): untracked and ignored files there (`.env`) are exactly what must not be published, and no caller needs it.
- An environment-variable override for the allowed roots (AD-5).
- Stopping a session that first copies a secret into the scratchpad with a separate command: that copy is its own tool call, seen by the Auto-mode classifier, and the same content could be passed to `mcp__github__update_issue_comment` directly. This plan closes the single allowlisted command that needed no prompt.

## Constraints

- §1: security first; the change only narrows what the helper reads.
- §5: the smallest change: one path check in the script, the tests, and the docs that describe the helper.
- §6: no identifier renamed or removed; `--body-file`, `--replacements`, `--dry-run`, exit codes, and output keys are unchanged. New names (`SCRATCHPAD_DIR_NAME`, `SESSION_TEMP_DIR_PREFIX`, `_temp_roots`, `is_scratchpad_path`, `read_input_file`) are checked for clashes in the module, in `check_in_status` (imported as a module, not star-imported), and in the tests.
- §9: tabs in Python.
- §14 / §20: the twin under `workflow-templates/.claude/` ships to consumers on the next `@stable` sync; one `changelog.d/` fragment (`security`).
- §15: no new API call; the check runs before the one comment read.
- §28.C: phase 1 edits `.claude/scripts/edit_comment.py` and `.claude/commands/implement-plan-claude.md`, protected paths. Twin-first while #4785 is open: only the `workflow-templates/.claude/` twins are edited, and the `.claude/` copy is a `[claude-twin-sync]` commit.

## Approach

Add `read_input_file(flag, path)` to the script. It resolves the path strictly, checks with `is_scratchpad_path` that the resolved path is `<temp root>/claude-<…>/<…>/scratchpad/<file>` for one of `_temp_roots()` (the resolved `tempfile.gettempdir()` and `/tmp`, de-duplicated), opens the resolved path with `O_NOFOLLOW`, and checks with `fstat` on the open descriptor that it is a regular file with `st_nlink == 1` before reading it as UTF-8. Any failure raises `ValueError`, which `main` already maps to exit 1 and a JSON `error`. `load_replacements` and the `--body-file` branch of `main` call it instead of `Path(...).read_text`. Both run before `edit_comment`, so a rejected path makes no API call.

Alternatives: allow the working tree too (AD-1 B, rejected: ignored secret files); drop `--body-file` (AD-3 C, rejected: §6); accept all of the temp directory (rejected: it holds session MCP configs and logs).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes exactly one phase.

1. **Phase 1 — restrict edit_comment.py input files to the session scratchpad.** protected paths: `.claude/scripts/edit_comment.py`, `.claude/commands/implement-plan-claude.md`.
   - Files: see Files & Modules.
   - Done: `tests/test_edit_comment.py` passes with the new cases (loading the module from the twin); the #5452 command shape (`--body-file` naming a file outside the scratchpad) exits 1 with no API call and no content in the output; a scratchpad file still edits the comment; CLAUDE.md §23.I, `agents.md`, and the Helpers section describe the rule; the changelog fragment exists. `test_template_parity` and the command-twin parity checks go green with the `[claude-twin-sync]` copy.
   - Rollback: revert the phase PR; the helper reads any path again.

## Implementation Steps

1. `workflow-templates/.claude/scripts/edit_comment.py`: add `import os`, `import stat`, the constants `SCRATCHPAD_DIR_NAME = "scratchpad"` and `SESSION_TEMP_DIR_PREFIX = "claude-"`, and `_temp_roots`, `is_scratchpad_path`, `read_input_file`; call `read_input_file` from `load_replacements` and from `main` for `--body-file`; describe the rule in the module docstring.
2. `tests/test_edit_comment.py`: load the module from the twin (AD-4); a fixture that points `_temp_roots` at `tmp_path` and builds `tmp_path/claude-0/<project>/<session>/scratchpad/`; move the existing input files into it; add the rejection cases from G2 (each asserting exit 1, no `gh_api` read, no PATCH, and no file content in the output, including with `--dry-run`), `is_scratchpad_path` unit cases (`/tmp/claude-code-1.diag.log`, `/tmp/mcp-config-x.json`, the scratchpad directory itself, a nested `other/claude-0/…/scratchpad/x`), and a `_temp_roots` case that includes the resolved `tempfile.gettempdir()`.
3. `CLAUDE.md` §23.I helper table: say the helper reads its input file only from the session scratchpad.
4. `agents.md` helper bullet: the same, with the rejection behaviour.
5. `workflow-templates/.claude/commands/implement-plan-claude.md` Comment helper: the files must be in the scratchpad; the helper rejects any other path; prefer `mcp__github__update_issue_comment` for a whole-body rewrite.
6. `changelog.d/5452-edit-comment-scratchpad-only.md` (`security`).
7. After the phase PR opens: the `[claude-twin-sync]` copy of steps 1 and 5 into `.claude/` (per the twin-first rule; `.claude/scripts/edit_comment.py` and `.claude/commands/implement-plan-claude.md`).

## Files & Modules

- `workflow-templates/.claude/scripts/edit_comment.py` → `.claude/scripts/edit_comment.py` (protected path, twin sync)
- `workflow-templates/.claude/commands/implement-plan-claude.md` → `.claude/commands/implement-plan-claude.md` (protected path, twin sync)
- `tests/test_edit_comment.py`
- `CLAUDE.md` (= `workflow-templates/CLAUDE.md`)
- `agents.md`
- `changelog.d/5452-edit-comment-scratchpad-only.md` [new]

## Tests

- Unit: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests/test_edit_comment.py` (already in its `ci.yml` step), with the cases in step 2.
- Replay: run the twin with `--body-file <a file outside the scratchpad> --dry-run` and with a scratchpad file `--dry-run`, and check the outputs.
- Regression: `tests/test_permission_prompts.py`, `tests/test_implement_plan_claude_command.py`, `tests/test_claude_md_section_numbers.py`.

## Risks & Mitigations

- A Claude Code version that moves the scratchpad out of `<tmp>/claude-*/…/scratchpad/` makes the helper reject every file. Mitigation: the error names the rule and points to `mcp__github__update_issue_comment`, which every caller already has; ACCEPTED — it fails closed.
- A session without a scratchpad (an older CLI, a human at a shell) cannot use the helper with a file elsewhere. ACCEPTED — the same fix: put the file in a scratchpad-shaped directory or use the MCP tool.
- A path swapped between the check and the open. Mitigation: the open uses `O_NOFOLLOW` on the resolved path and the checks run on the open descriptor; ACCEPTED for directory swaps higher up, which need a separate command in the same session.
- Consumer repos receive the change with the next `@stable` sync (§14); ACCEPTED — it only narrows behaviour.

## Rollout

No flag. The script ships to consumers with the `.claude/` sync. Rollback is a revert of the phase PR.

## References

- #5452 (this issue), #4619 (the `gh api -F @file` fix), #4785 (twin sync)
- CLAUDE.md §6, §23.D, §23.H, §23.I, §28.C

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which files may `--body-file` and `--replacements` read? — Picked: A — only files resolving under a Claude Code session scratchpad (`<temp root>/claude-*/…/scratchpad/`). Alternatives: B — the scratchpad plus the git working tree; C — the whole temp directory. Why: the documented flow already uses the scratchpad; the working tree can hold ignored secret files (`.env`), and `/tmp` holds session MCP configs and logs. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How strictly is the file itself checked? — Picked: A — resolve symlinks first, open the resolved path with `O_NOFOLLOW`, and require a regular file with one hard link on the open descriptor. Alternatives: B — resolve symlinks only. Why: a hard link in the scratchpad to a secret file passes a path check, and a FIFO or device could block or leak; the extra checks cost two lines. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Keep or drop `--body-file`? — Picked: A — keep it, restricted like `--replacements`, and point whole-body rewrites at `mcp__github__update_issue_comment` in the docs. Alternatives: B — deprecate it with a warning; C — remove it. Why: §6 forbids removing a CLI flag without the ask flow, and restricted it no longer leaks. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Which copy do the behaviour tests load while `.claude/` waits for the twin sync? — Picked: A — the `workflow-templates/.claude/scripts/` twin, with `test_template_parity` still comparing both copies. Alternatives: B — keep loading `.claude/scripts/edit_comment.py`. Why: the twin-first rule (CLAUDE.md §28.C interim) says tests read the twin so they pass before the sync; after it both copies are identical. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Add an environment variable to widen the allowed roots? — Picked: A — no. Alternatives: B — `EDIT_COMMENT_ALLOWED_DIRS`. Why: an override would reopen the path for any command that sets it, and the §23.H guard deliberately has no escape hatch either. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Exit code for a rejected path? — Picked: A — 1 (invalid argument), before any API call, with a JSON `error`. Alternatives: B — 2 (call failed). Why: the documented contract uses 1 for an invalid argument and 2 for a failed call; nothing was called. Applied in: phase 1 PR. Status: pending review

## Notes

- The session that implements this runs in Auto mode (`permission_mode: auto`).
- `security_pass_skip.py` (2026-09-30): `{"skip": false, "label": null, "reason": "no skip label"}`.
