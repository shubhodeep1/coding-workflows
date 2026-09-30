# Implement-Plan Log — Guard differential: a warning no longer excuses a loosened guard

- Plan: docs/plans/issue-5325-fail-warned-guard-loosening-plan.md
- Source issue: shubhodeep1/coding-workflows#5325   Progress comment: 5902398794
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5325-fail-warned-guard-loosening   Final PR: #5363 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5366
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_019WGh3J49ycyYL7KPVjubhj   safety net / hand-back: see the review-round 1 stage report
- Last updated: 2026-09-30
- Last note: review round 1 on PR #5366: fixed the stale docstring summary line (task gap), rejected 3 findings on pre-existing #5174 sandbox code (out of scope or misread); 72 tests pass, b6dd693 exits 1 and 03c2487 exits 0 against f736cad.

## Phases
1. [ ] Phase 1 — fail on a warned loosening (scripts/guard_differential.py rule, tests, docstring, remediation text, ci.yml comment, agents.md, #5174 changelog fragment)   — PR #5366 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` returned skip: true)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How far should a warning count once the head is less strict? — Picked: A — not at all: every drop in strictness is a regression, and the warning is only printed. Alternatives: B — keep the exemption when the head answers `ask` or `none`; C — keep the exemption only for `ask`. Why: §1; a `none` with a warning still hands a formerly blocked shape to the normal permission flow, and the issue asks for failure "regardless of `systemMessage`". Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which changelog entry records the fix? — Picked: A — correct the one phrase in the unreleased `changelog.d/5174-guard-differential-check.md` and add no new fragment. Alternatives: B — add `changelog.d/5325-fail-warned-guard-loosening.md` with `<!-- changelog: security -->` as well. Why: the warning exemption never reached `main` or `stable`, so a separate "fixed" entry would describe a behaviour no consumer saw (§20.F), while the #5174 fragment would otherwise describe the wrong rule. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should the #5174 plan and progress log be updated to the new rule? — Picked: A — no, they stay as that project's record; the rule is corrected in the code, tests, `agents.md`, `ci.yml`, and the changelog fragment. Alternatives: B — also edit the #5174 plan's Approach paragraph. Why: §5, and those files record what #5174 planned and did. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] When a change reverses a rule, grep the whole module docstring, its summary line included, for wording tied to the old rule, not only the paragraph the plan names. (files: scripts/guard_differential.py)

## Notes
- Base branch is the open #5174 project branch (final PR #5185, draft); every stage checks whether it merged first (Issue Mode "A base branch that merges moves the project").
- No phase touches `.claude/**`, so no protected-path approval is needed.
