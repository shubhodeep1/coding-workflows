# Implement-Plan Log — Stop legacy filed counts from hiding real permission-prompt denials

- Plan: docs/plans/issue-5012-legacy-outage-filed-counts-plan.md
- Source issue: shubhodeep1/coding-workflows#5012
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4750-classifier-outage-get-session
- Project branch: claude/implement-plan-issue-5012-legacy-outage-filed-counts   Final PR: #5028 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (twin sync by the master session, Q40)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: phase 1 implemented twin-first (Q40): only `workflow-templates/.claude/scripts/permission_prompts.py` changed under `.claude`; phase PR opened with a `hold` claim; waiting for the `[claude-twin-sync]` into `.claude/scripts/permission_prompts.py`

## Phases
1. [ ] Phase 1 — record-level filed state with legacy migration — protected paths: .claude/scripts/permission_prompts.py (edited only in its workflow-templates/.claude/ twin, Q40) — PR open, awaiting twin sync; review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; security_pass_skip.py verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should the filer stop legacy counts from hiding real denials? — Picked: A — track filed records by key (`<log file>:<line>`) per signature in a versioned `filed-state.json`, migrating legacy counts by the load-order prefix. Alternatives: B — keep counts and subtract each signature's outage records once; C — discard legacy state. Why: A is exact for both old and new state and is the issue's second recommendation; B is wrong when outages arrived after the last filing; C re-comments every pattern. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is an unversioned state treated when it may have been written by the unreleased #4750 code (outage-exclusive counts)? — Picked: A — always as outage-inclusive (the released behaviour). Alternatives: B — as outage-exclusive; C — guess per signature. Why: A can only over-report (one extra comment), B can still hide real denials, which is the defect. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Does the state file keep its name? — Picked: A — keep `filed-state.json`, add `"version": 2`. Alternatives: B — a new file name. Why: §6; the version key tells the formats apart. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:security] A filer state that stores cumulative per-pattern counts breaks silently when the grouping later drops records; key filed state by stable record identity (file + line for append-only logs) and migrate old counts by their load-order prefix. (files: .claude/scripts/permission_prompts.py)

## Notes
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29) (operator comment 5883926767 on #5012).
- Blocked 2026-09-29 before phase 1: protected-path approval asked on #5012 (`ai:claude-blocked`). Answer with a `Protected-path approval: phase 1 — <letter> (<date>)` line and `/reclarify`.
- This log update was pushed directly to the project branch as part of step 3a (no phase PR is in flight to carry it), so a resumed stage sees `Final PR: #5028` and does not open a second final PR.
- Started by the Claude issue dispatcher routine (trigger trig_01FzNj7jt3iTsxF8CUydykuf) in session session_01FxPzZDsebojWQPAqJirYk1; `gh` was installed by running `.claude/hooks/session-start.sh` because the repository was attached mid-session; the GitHub MCP tools were not available, so issue and PR writes used `gh api` REST calls the §23.H guard classifies as routine.
