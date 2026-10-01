# Implement-Plan Log — Bound how long a hold can park the project checker

- Plan: docs/plans/issue-5927-bound-plain-mode-hold-plan.md
- Source issue: shubhodeep1/coding-workflows#5927
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5927-bound-plain-mode-hold   Final PR: (opening)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: Project started from issue #5927 on base claude/implement-plan-issue-5667-checker-waits-on-held-head.

## Phases
1. [ ] Phase 1 — plain PR mode bounds a hold by age — protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/implement-plan-claude.md` (twins only)
   - [ ] `check_pr` in `workflow-templates/.claude/scripts/check_in_status.py`: a trusted hold on the current head waits only while younger than `CLAUDE_FIX_HOLD_MAX_HOURS` (default 24); an older or undatable hold reports `state: blocked` (done) with `head_sha` and `claim`; module docstring updated
   - [ ] Command twin: done-waiting *PR* bullet **Held** names the bound and what a stale hold routes to
   - [ ] Tests against the twin in `tests/test_check_in_status.py` (fresh / stale / env override / bad timestamp / call budget / `--hand-back` unchanged)
   - [ ] Docs: CLAUDE.md §26.H (both copies), `agents.md`, `README.md`; `changelog.d/5927-bound-plain-mode-hold.md` and the #5667 fragment's row
   - [ ] `[claude-twin-sync]` copy of both twins into `.claude/` (human, after the twin-sync blocker)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] How should plain PR mode stop a hold from parking the project checker indefinitely? — Picked: A — bound the hold by age: it waits only while younger than `CLAUDE_FIX_HOLD_MAX_HOURS` (default 24), then routes as blocked. Alternatives: B — let blocking labels outrank a hold again; C — require a matching `ai:claude-blocked` blocker on the source issue; D — trust holds only from `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN`. Why: A ends the indefinite wait with no new API call and keeps #5667's fix for real twin-sync waits (§1, §5, §15). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] What does a stale hold report? — Picked: A — `state: blocked` (done), routed to `hand_back` by the existing table. Alternatives: B — ignore the stale hold and route as if there were none. Why: B restarts a review-round stage every hour on a head still waiting for a twin sync; A uses the bounded blocked-PR intervention. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] Default limit for `CLAUDE_FIX_HOLD_MAX_HOURS`? — Picked: A — 24 hours. Alternatives: B — 72 hours; C — 6 hours. Why: matches the chain's 24-hour safety net and is well above the observed twin-sync waits. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Should `--hand-back` mode bound holds too? — Picked: A — no, plain mode only. Alternatives: B — bound both. Why: the finding is the project checker's plain mode; the §26.H cap hold is a documented "never expires on the same head" contract (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-10-01] Which hold's age counts? — Picked: A — the latest trusted hold on the current head, with a missing or unparseable time counted as stale. Alternatives: B — the earliest trusted hold on the head. Why: the latest claim decides the state, A needs no new parsing, and fail-closed keeps the bound. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] How does the changelog record the change? — Picked: A — a new `security` fragment and a one-row correction to the #5667 fragment. Alternatives: B — only the new fragment. Why: the #5667 row would otherwise state the old rule. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Security pass: skip — `security_pass_skip.py` verified #5927 as an automation-produced `ai:security` issue.
- Issue base `claude/implement-plan-issue-5667-checker-waits-on-held-head` is the head of open draft PR #5684 into `main`; check at every stage whether it merged (Issue Mode, "A base branch that merges moves the project").
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
