# Implement-Plan Log — Guard: deny inline-interpreter file edits instantly and redirect to the Edit tool

- Plan: docs/plans/issue-4858-inline-edit-guard-plan.md
- Source issue: shubhodeep1/coding-workflows#4858
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4858-inline-edit-guard   Final PR: #F draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: operator answer on #4858 (protected-path approval for phase 1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: Phase 1 edits `.claude/**` (protected paths). The log has no `Protected-path approval:` line and #4785 (twin-first delivery) is not on `main`, so the phase was not started (CLAUDE.md §28.C). The ask is posted on #4858 with the `ai:claude-blocked` label.

## Phases
1. [ ] Phase 1 — inline-edit guard hook, wiring, logging, docs, tests   — protected paths: `.claude/hooks/inline_edit_guard.py`, `.claude/settings.json`, `.claude/scripts/permission_prompts.py`
   - [ ] `inline_edit_guard.py` (both copies, byte-identical): deny with the issue's message; no decision for reads, `pytest`, file-path scripts, and data; fail open; `CLAUDE_INLINE_EDIT_GUARD=off`
   - [ ] Both `settings.json` copies wire it as a `PreToolUse` `Bash` hook
   - [ ] Denies log `INLINE_EDIT_GUARD action=deny` and write a `source: inline_edit_guard` record; `permission_prompts.py` (both copies) never files them
   - [ ] `tests/test_inline_edit_guard.py` plus the `ci.yml` step; `tests/test_permission_prompts.py` exclusion case
   - [ ] CLAUDE.md §23.I (both copies) and `agents.md` document the guard
   - [ ] `changelog.d/4858-inline-edit-guard.md`
   - Done: the new and existing guard/prompt tests pass, and the twins are byte-identical

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does a deny get recorded for `permission_prompts.py`? — Picked: A — the guard appends a `PermissionDenied` record with `source: "inline_edit_guard"` through `permission_prompt_logger.py`'s own functions, and `permission_prompts.py` skips that source when filing. Alternatives: B — rely on Claude Code firing `PermissionDenied` for hook denies; C — a separate log file. Why: it reuses the existing log and makes the exclusion explicit and testable. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How should `sed -i` / `perl -i` / `ruby -i` / `awk -i inplace` be matched? — Picked: A — deny on the in-place flag alone. Alternatives: B — also require a write pattern in the program text. Why: the flag is the write. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] How does the new hook reuse `gh_api_write_guard.py`'s tokenizer? — Picked: A — load it by path with `importlib.util` and fall back to no decision if that fails. Alternatives: B — copy the functions; C — extract a shared module. Why: it is the smallest change that keeps one tokenizer. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] Which base does this project use, given the issue expects #4785 (twin-first) and #4678 (§23.I rule), neither on `main`? — Picked: A — `main`, with steps written to work in either merge order. Alternatives: B — base on #4785's project branch. Why: base resolution is rule-defined when the issue names no integration branch. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Permission mode at start: auto.
- The planning session had no `gh` CLI and no `mcp__github__*` tools. GitHub reads and writes went through REST (`curl`) via the session proxy. `security_pass_skip.py` exited 2 for that reason, so `Security pass: run` (the issue carries no skip label either).
- Stale Routine sweep: 10 of the 12 Routines it selected were already gone; the Auto-mode classifier denied deleting `trig_01EBhQYhRYN8Zscd6Jmo2oBr` and `trig_01GsBkfzNveyKjbEZtAWKaEf` (both ended one-shots), which were left in place.
- Phase 1 not started: protected paths, no `Protected-path approval:` line (CLAUDE.md §28.C). The issue expects the twin-first path from #4785 (final PR #4804, draft) and the §23.I rule text from #4678; neither is on `main` as of 2026-09-29.
