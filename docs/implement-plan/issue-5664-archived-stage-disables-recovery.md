# Implement-Plan Log — Keep issue-start sessions until their issue closes (archived-stage-disables-recovery)

- Plan: docs/plans/issue-5664-archived-stage-disables-recovery-plan.md
- Source issue: shubhodeep1/coding-workflows#5664 (https://github.com/shubhodeep1/coding-workflows/issues/5664)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4887-archive-finished-sessions
- Project branch: claude/implement-plan-issue-5664-archived-stage-disables-recovery   Final PR: #5674 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1/1 (session_01AZY542gSPU8zpztp9mNUSh): project branch and draft final PR #5674 opened; phase 1 implemented (supersede rule removed, 6 new/updated tests fail on the old script and pass on the new one).

## Phases
1. [ ] Phase 1 — issue-start sessions archive only on a closed issue
   - `scripts/claude_session_janitor.py`: drop the supersede rule (`classify()`), docstring
   - `tests/test_claude_session_janitor.py`: failed-start and live-stage keep tests; pickup-titles test
   - CLAUDE.md §26.I, `README.md`, `agents.md`, `changelog.d/4887-archive-finished-sessions.md`
   - `docs/plans/issue-4887-archive-finished-sessions-plan.md`: one Notes line
   - Done: janitor suite, changelog fragment contract, and section-number tests pass; ruff clean

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How does the sweep make sure a later stage really replaced the issue-start session before archiving it? — Picked: A — it no longer tries: the supersede rule is removed, and an issue-start session is archived only when its issue is closed. Alternatives: B — a complete `list_triggers` listing passed as `--triggers` (several pages per wake, since 100+ Routines are enabled, and a no-twin pickup edit); C — a session-data heuristic. Why: the finding says keep the predecessor when a check is uncertain; A is the smallest fail-safe change. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to the stage-title parsing and `NON_SUPERSEDING_STAGE_PATTERN`? — Picked: A — keep both, the constant with a comment. Alternatives: B — delete the constant. Why: §6 / §28.B. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Where does the changelog record this? — Picked: A — correct the unreleased `changelog.d/4887-archive-finished-sessions.md`. Alternatives: B — a new `changelog.d/5664-…` security fragment. Why: both reach `main` together through #4924. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Does project #4887's plan learn about the narrowed rule? — Picked: A — one `## Notes` line. Alternatives: B — leave it. Why: stops a later #4887 conformance run from restoring the rule. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Security pass skip reason: `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
