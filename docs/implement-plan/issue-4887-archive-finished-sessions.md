# Implement-Plan Log — Archive finished fixer, issue-start and report sessions automatically

- Plan: docs/plans/issue-4887-archive-finished-sessions-plan.md
- Source issue: shubhodeep1/coding-workflows#4887 (https://github.com/shubhodeep1/coding-workflows/issues/4887)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4887-archive-finished-sessions   Final PR: #4924 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #4937
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_019gAFuNuVjyEnPRhyvMWZzG   safety net trig_017cFjn71H85z5p1GwNWmcU7   hand-back trig_01JhVyrQS1gRyuz79G3cLXeT
- Last updated: 2026-09-29
- Last note: phase 1 pushed as PR #4937 twin-first; review round 1 on b7443b5 judged by a /fix-claude-pr session (all 5 findings and the task gap invalid, issue comment 5885528797); the supervising session pushed the [claude-twin-sync] commit e0d90bb; review round 2 runs on it; wait armed.

## Phases
1. [ ] Phase 1 — session sweep, fixer self-archive, docs   — PR #4937 open (waiting); review rounds: 1 (round 1, 2026-09-29: all findings rejected by the fixer, no autofix commit); twin sync e0d90bb; interventions: 0 — protected paths: `.claude/commands/fix-claude-pr.md`, `.claude/settings.json` (edited through their `workflow-templates/.claude/` twins), `.claude/commands/claude-issue-pickup.md` (no twin; exact edit in the blocked comment)
   - `scripts/claude_session_janitor.py` [new] + `tests/test_claude_session_janitor.py` [new] + `ci.yml` step
   - `fix-claude-pr.md` twin: terminal hand-back archives the fixer; step 8 wording
   - `settings.json` twin: allow `scripts/claude_session_janitor.py`
   - pickup step 3a (sweep), step 0 tools, step 4 report, Rules, Tool Access
   - CLAUDE.md §26.D + new §26.I; `README.md`, `agents.md`; `changelog.d/4887-archive-finished-sessions.md` [new]
   - Done: janitor tests pass; instruction-text tests pass with the twins and the pickup edit applied; ruff clean; section-number test passes

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where does the sweep's decision script live? — Picked: A — a new `scripts/claude_session_janitor.py`, coding-workflows-only like `scripts/claude_issue_route.py`. Alternatives: B — `.claude/scripts/stale_sessions.py` plus a twin; C — a subcommand of `scripts/claude_issue_route.py`. Why: no protected path, no consumer copy, tests run before the sync. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How does one page per wake reach older sessions? — Picked: A — a `next_after_id` cursor the pickup passes as `after_id` next wake, reset at the end or a 30-day horizon. Alternatives: B — newest page only; C — every page every wake. Why: keeps the §15 budget and reaches 7-day-old sessions. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Which waiting sessions are left alone? — Picked: A — `RUNNING` / `REQUIRES_ACTION` always; `need_input` keeps only report sessions. Alternatives: B — protect only `REQUIRES_ACTION`; C — protect every `need_input`. Why: reconciles the decision with the acceptance line. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] What is "superseded"? — Picked: A — a later non-checker `implement-plan issue-<N>-…` stage session for the same repo and issue on the page. Alternatives: B — any newer session for the issue. Why: a stood-down duplicate dispatch must not archive the active session; #4817 owns replacement. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] How does the sweep avoid racing the checker's terminal hand-back? — Picked: A — a 2-hour grace. Alternatives: B — a `list_triggers` Routine guard; C — 24 hours. Why: the fixer archives itself on the hand-back; the sweep catches the rest. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-29] How is the report's 7 days measured? — Picked: A — session `updated_at`. Alternatives: B — PR merged/closed time over REST. Why: no API call, and use of the session restarts the week. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-29] What does a fixer do on a terminal hand-back? — Picked: A — bookkeeping, a one-line reply, self-archive; no report. Alternatives: B — keep the report, sweep after 7 days. Why: the issue's shape and acceptance. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-29] Archive straight from the list? — Picked: A — no, `get_session` first, archive only if still IDLE with the same title. Alternatives: B — archive directly. Why: §1. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- 2026-09-29 twin sync: the supervising session copied `fix-claude-pr.md` and `settings.json` byte-for-byte from their twins and wrote its own `claude-issue-pickup.md` step 3a (the blocker comment was never posted: this session went idle at 02:26 UTC before posting it). Its step 3a matches the plan and additionally carries `next_after_id` in the one-line report so the cursor survives summarisation; 924 related tests pass on e0d90bb.
- 2026-09-29 phase 1 evidence: tests/test_claude_session_janitor.py 83 passed (pickup-text test awaits sync); 22 related suites on the unsynced tree fail only the expected parity/pickup tests; with the twins copied and the pickup diff applied, 1346 passed, 0 failed; inventory parity OK; ruff and yamllint clean; live dry run on the newest 100 sessions names 3 closed-issue issue-start sessions (#4881, #4688, #4857), no errors.
- Interim twin-first rule (#4750 Q40: A, restated in #4887): `.claude/**` changes land through `workflow-templates/.claude/**` twins; the phase PR carries a `hold` claim and the stage stops BLOCKED for the supervising session's `[claude-twin-sync]`.
- Overlap: `docs/plans/claude-fixer-unattended-convergence-plan.md` phase 4 (not started) plans `.claude/scripts/stale_sessions.py`; it should extend `scripts/claude_session_janitor.py` instead of adding a second janitor.
