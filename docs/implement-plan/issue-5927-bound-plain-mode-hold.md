# Implement-Plan Log — Bound how long a hold can park the project checker

- Plan: docs/plans/issue-5927-bound-plain-mode-hold-plan.md
- Source issue: shubhodeep1/coding-workflows#5927
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5927-bound-plain-mode-hold   Final PR: #5940 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5944: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: Phase 1 PR #5944 opened twin-first; hold claim posted on its head and the twin-sync blocker posted on issue #5927. The project resumes on `/reclarify` after the `[claude-twin-sync]` push.

## Phases
1. [ ] Phase 1 — plain PR mode bounds a hold by age — PR #5944 open (twin sync pending); review rounds: 0; interventions: 0 — protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/implement-plan-claude.md` (twins only)
   - [x] `check_pr` in `workflow-templates/.claude/scripts/check_in_status.py`: a trusted hold on the current head waits only while younger than `CLAUDE_FIX_HOLD_MAX_HOURS` (`DEFAULT_FIX_HOLD_MAX_HOURS` `:161`; check `:275-288`); an older or undatable hold reports `state: blocked` (done) with `head_sha` and `claim`; module docstring (`:51-59`) and inline comment updated
   - [x] Command twin: done-waiting *PR* bullet **Held** names the bound and what a stale hold routes to (`workflow-templates/.claude/commands/implement-plan-claude.md:223`)
   - [x] Tests against the twin in `tests/test_check_in_status.py:1023-1127` (fresh / stale / env override / bad timestamp / newer hold / call budget / `--hand-back` unchanged / command twin); 18 new cases fail against the old script and pass against the twin
   - [x] Docs: CLAUDE.md §26.H (`workflow-templates/CLAUDE.md` is a symlink to it), `agents.md:1024,1067`, `README.md:92` (env var row) and `:1477`; `changelog.d/5927-bound-plain-mode-hold.md` (`security`) and the #5667 fragment's "What outranks a hold" row
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

- AD-7 [phase 1/1, 2026-10-01] The base project (#5667, its AD-7) deferred a wording fix ("until a push moves the head" omits "or a newer trusted claim on the same head") in the `check_in_status.py` docstring and inline comment and the command's **Held** bullet to the next `.claude/` edit of these files. This phase edits exactly those lines. Include the fix? — Picked: A — yes, in the same twin edits. Alternatives: B — leave it for later. Why: the lines are rewritten here anyway, so B would leave known-stale text in the touched files at no saving (§12.B stale docs). Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Security pass: skip — `security_pass_skip.py` verified #5927 as an automation-produced `ai:security` issue.
- Issue base `claude/implement-plan-issue-5667-checker-waits-on-held-head` is the head of open draft PR #5684 into `main`; check at every stage whether it merged (Issue Mode, "A base branch that merges moves the project").
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
