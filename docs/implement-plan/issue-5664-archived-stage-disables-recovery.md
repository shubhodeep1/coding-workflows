# Implement-Plan Log — Keep issue-start sessions until their issue closes (archived-stage-disables-recovery)

- Plan: docs/completed/issue-5664-archived-stage-disables-recovery-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5664 (https://github.com/shubhodeep1/coding-workflows/issues/5664)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4887-archive-finished-sessions
- Project branch: claude/implement-plan-issue-5664-archived-stage-disables-recovery   Final PR: #5674 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01PVfNdjDT88NAoRDkrYdBzR
- Last updated: 2026-10-01
- Last note: validation cycle 1/3 (run 36808191924) read: status=pass raw_status=pass, 10/10 tests on project-branch head e828160; completion PR moves the plan to docs/completed/ (session_013naezmvKCXFMazFpTWRf6A).

## Phases
1. [x] Phase 1 — issue-start sessions archive only on a closed issue
   - `scripts/claude_session_janitor.py`: drop the supersede rule (`classify()`), docstring
   - `tests/test_claude_session_janitor.py`: failed-start and live-stage keep tests; pickup-titles test
   - CLAUDE.md §26.I, `README.md`, `agents.md`, `changelog.d/4887-archive-finished-sessions.md`
   - `docs/plans/issue-4887-archive-finished-sessions-plan.md`: one Notes line
   - PR #5694 merged 2026-10-01 as e828160 (by the master session, operator Q46: A); review rounds: 3; interventions: 0
   - Done: janitor suite, changelog fragment contract, and section-number tests pass; ruff clean

## Conformance
- Run 1 — 2026-10-01: CONFORMANT — no fixes (pre-security)

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation
- Cycle 1 — run 36804419254 2026-10-01 (target_ref: claude/implement-plan-issue-5664-archived-stage-disables-recovery): failed before validating (GH_PAT hourly API budget, #5504); not counted per Q1: A on #5664
- Cycle 1 — run 36808191924 2026-10-01 (target_ref: claude/implement-plan-issue-5664-archived-stage-disables-recovery, head e828160): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 271s); no fix issues

## Completion
- Completion PR — doc moved to docs/completed/issue-5664-archived-stage-disables-recovery-plan.md
- Final PR #5674 draft (base claude/implement-plan-issue-4887-archive-finished-sessions)

## Activation
- n/a until the final merge: the issue base is project #4887's branch, so this project ends with `Activation: n/a (base claude/implement-plan-issue-4887-archive-finished-sessions)` after #5674 merges, unless that base moves to `main` first (Issue Mode)

## Auto-decisions
- AD-1 [plan, 2026-09-30] How does the sweep make sure a later stage really replaced the issue-start session before archiving it? — Picked: A — it no longer tries: the supersede rule is removed, and an issue-start session is archived only when its issue is closed. Alternatives: B — a complete `list_triggers` listing passed as `--triggers` (several pages per wake, since 100+ Routines are enabled, and a no-twin pickup edit); C — a session-data heuristic. Why: the finding says keep the predecessor when a check is uncertain; A is the smallest fail-safe change. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What happens to the stage-title parsing and `NON_SUPERSEDING_STAGE_PATTERN`? — Picked: A — keep both, the constant with a comment. Alternatives: B — delete the constant. Why: §6 / §28.B. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Where does the changelog record this? — Picked: A — correct the unreleased `changelog.d/4887-archive-finished-sessions.md`. Alternatives: B — a new `changelog.d/5664-…` security fragment. Why: both reach `main` together through #4924. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Does project #4887's plan learn about the narrowed rule? — Picked: A — one `## Notes` line. Alternatives: B — leave it. Why: stops a later #4887 conformance run from restoring the rule. Applied in: phase 1. Status: pending review

## Lessons
- [source:intervention] When a rule changes, grep every operator-facing doc for its old wording, including docs/operations/master-session.md standing decisions, not only the files the plan lists. (files: docs/operations/master-session.md)
- [source:intervention] When a rule narrows, update the summary paragraph at the top of each description (module docstring, section intro, changelog lead) as well as the detailed rule below it; reviewers read the two against each other. (files: scripts/claude_session_janitor.py, CLAUDE.md)

## Notes
- Security pass skip reason: `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Completion PR also points the #5664 Notes line in `docs/plans/issue-4887-archive-finished-sessions-plan.md` at the moved plan (`docs/completed/`).
