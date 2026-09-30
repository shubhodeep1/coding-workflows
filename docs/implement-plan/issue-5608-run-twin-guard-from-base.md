# Implement-Plan Log — Run the Claude twin sync-state guard from the protected base commit, not the PR checkout

- Plan: docs/plans/issue-5608-run-twin-guard-from-base-plan.md
- Source issue: shubhodeep1/coding-workflows#5608
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-4785-twin-first-claude-sync
- Project branch: claude/implement-plan-issue-5608-run-twin-guard-from-base   Final PR: #5653 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (branch claude/implement-plan-issue-5608-run-twin-guard-from-base-phase-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented (ci.yml runs the base commit's claude_twin_sync.py; 8 new tests; 149 twin sync tests pass, the 7 behaviour tests fail on the old step); phase PR opened, waiting on its review.

## Phases
1. [ ] Phase 1 — run the twin sync-state guard from the base commit (protected paths: none)   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` printed `"skip": true`).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which commit supplies the guard script the step runs? — Picked: A — the event's `base` commit (the merge commit's first parent, or `github.event.before`), with `main`'s tip as the fallback on `stable`. Alternatives: B — always `main`'s tip; C — a `pull_request_target` workflow. Why: the smallest change that runs only protected-branch content, using commits the step already fetches (§1, §5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What does the step do when the trusted commit resolves but has no `scripts/claude_twin_sync.py`? — Picked: A — log a `::warning::` and run the checkout's copy, as today. Alternatives: B — skip the check; C — fail closed. Why: a base without the script has no guard to bypass; A keeps today's coverage while an unreadable commit still fails closed (§1, §3). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Should this project also stop a PR from editing `.github/workflows/ci.yml` to drop the step? — Picked: A — no; record it as the remaining gap in `agents.md`. Alternatives: B — a `pull_request_target` re-check workflow. Why: the finding names the script; B is a new privileged workflow needing its own design and an operator ruleset change (§5, §23.C). Applied in: phase 1 (docs only). Status: pending review

## Lessons

## Notes
- Issue progress comment: 5911158781.
- Issue mode: base is not the default branch, so the final-merge stage closes #5608 with `state_reason: completed` and `ai:merged`; Activation n/a.
