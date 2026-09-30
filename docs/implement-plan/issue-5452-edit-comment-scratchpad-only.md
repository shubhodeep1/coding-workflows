# Implement-Plan Log — edit_comment.py: read `--body-file` and `--replacements` only from the session scratchpad

- Plan: docs/plans/issue-5452-edit-comment-scratchpad-only-plan.md
- Source issue: shubhodeep1/coding-workflows#5452
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5452-edit-comment-scratchpad-only   Final PR: #5464 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5465: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 PR #5465 opened twin-first; hold claim posted; waiting on the `[claude-twin-sync]` copy of 2 `.claude/` files (blocker on #5452), then `/reclarify`.

## Phases
1. [ ] Phase 1 — restrict edit_comment.py input files to the session scratchpad   — PR #5465 open (hold: twin sync); review rounds: 0; interventions: 0; protected paths: `.claude/scripts/edit_comment.py`, `.claude/commands/implement-plan-claude.md`
   - [x] twin `workflow-templates/.claude/scripts/edit_comment.py`: `read_input_file` / `is_scratchpad_path` / `_temp_roots`, used by `load_replacements` and `--body-file`
   - [x] `tests/test_edit_comment.py`: load the twin; scratchpad fixture; rejection and unit cases
   - [x] CLAUDE.md §23.I, `agents.md`, twin `implement-plan-claude.md` Comment helper
   - [x] `changelog.d/5452-edit-comment-scratchpad-only.md` (`security`)
   - [ ] `[claude-twin-sync]` copy into `.claude/` (after the phase PR opens)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which files may `--body-file` and `--replacements` read? — Picked: A — only files resolving under a Claude Code session scratchpad (`<temp root>/claude-*/…/scratchpad/`). Alternatives: B — the scratchpad plus the git working tree; C — the whole temp directory. Why: the documented flow already uses the scratchpad; the working tree can hold ignored secret files (`.env`), and `/tmp` holds session MCP configs and logs. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How strictly is the file itself checked? — Picked: A — resolve symlinks first, open the resolved path with `O_NOFOLLOW`, and require a regular file with one hard link on the open descriptor. Alternatives: B — resolve symlinks only. Why: a hard link in the scratchpad to a secret file passes a path check, and a FIFO or device could block or leak; the extra checks cost two lines. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Keep or drop `--body-file`? — Picked: A — keep it, restricted like `--replacements`, and point whole-body rewrites at `mcp__github__update_issue_comment` in the docs. Alternatives: B — deprecate it with a warning; C — remove it. Why: §6 forbids removing a CLI flag without the ask flow, and restricted it no longer leaks. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Which copy do the behaviour tests load while `.claude/` waits for the twin sync? — Picked: A — the `workflow-templates/.claude/scripts/` twin, with `test_template_parity` still comparing both copies. Alternatives: B — keep loading `.claude/scripts/edit_comment.py`. Why: the twin-first rule (CLAUDE.md §28.C interim) says tests read the twin so they pass before the sync; after it both copies are identical. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Add an environment variable to widen the allowed roots? — Picked: A — no. Alternatives: B — `EDIT_COMMENT_ALLOWED_DIRS`. Why: an override would reopen the path for any command that sets it, and the §23.H guard deliberately has no escape hatch either. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Exit code for a rejected path? — Picked: A — 1 (invalid argument), before any API call, with a JSON `error`. Alternatives: B — 2 (call failed). Why: the documented contract uses 1 for an invalid argument and 2 for a failed call; nothing was called. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A helper allowlisted in `.claude/settings.json` that reads a caller-supplied file path must confine it (resolve symlinks, reject hard links and non-regular files) before the read, or it becomes a promptless exfiltration path. (files: .claude/scripts/edit_comment.py, .claude/settings.json)

## Notes
- Permission mode: auto (issue mode records it; §28.A).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- Security pass: run (`security_pass_skip.py`: no skip label).
- Twin sync pending (2026-09-30): `workflow-templates/.claude/scripts/edit_comment.py` (sha256 bff0569b53d3f6b0abbd1078eea898614f8beae44cfad73fe899b7b75e4bc06e) → `.claude/scripts/edit_comment.py`; `workflow-templates/.claude/commands/implement-plan-claude.md` (sha256 c6f55a6fd3abcdad91b90ff9337e4f7d6452fe02fb325fb86eed92732d07b8d8) → `.claude/commands/implement-plan-claude.md`.
