# Implement-Plan Log — Pending-checks auto-merge: any failed newer review blocks the merge

- Plan: docs/plans/issue-5904-block-merge-on-any-failed-newer-review-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5904-block-merge-on-any-failed-newer-review   Final PR: #5914 draft
- Source issue: shubhodeep1/coding-workflows#5904   Issue base: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (number in the stage report and the checker's resume block)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: armed after the phase 1 PR opened (ids in the stage report)
- Last updated: 2026-10-01
- Last note: phase 1 implemented and verified (142 pending-checks tests; the new #5904 cases fail on the old code); phase PR opened, waiting on its review

## Phases
1. [ ] Phase 1 — any unsuccessful newer bound review blocks the pending-checks merge   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (plan header `Security pass: skip (ai:security: automation-produced issue)`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] How should a later `success` run relate to an earlier failed newer review? — Picked: A — every completed bound review run newer than the marker's run must have concluded `success`; one that did not blocks until a newer marker (from a successful full review) moves the marker's run id past it. Alternatives: B — keep the latest-run rule but detect and ignore gate-skipped runs by reading each run's jobs; C — require the marker's run to be the latest completed review. Why: the issue's recommendation, no new reads (§15), and every successful full review leaves its own marker, hand-off, or auto-merge. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Should the review gate also stop skipping routine dispatches on a pending-checks head with a failed newer review? — Picked: A — no; change only the evaluator and document the recovery (a push, a base change, or the `force-review` label). Alternatives: B — teach `gate_claude_pending_checks_on_head` to list the PR's review runs. Why: §5 and the issue's scope; the stuck state already existed when the failed run was the latest. Applied in: phase 1 PR (docs only). Status: pending review
- AD-3 [plan, 2026-10-01] Should a completed unbound review dispatch that failed also block? — Picked: A — no, unchanged. Alternatives: B — block on any newer failed unbound dispatch in the repository. Why: those runs carry no PR binding, so B would stall every pending merge in the repo. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-01] Which changelog section? — Picked: A — `security`. Alternatives: B — `fixed`. Why: it closes an access-control finding from the security audit. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A). Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5904#issuecomment-5925634297
- The issue base is not the default branch, so the final-merge stage closes #5904 explicitly and adds `ai:merged`; steps 12–13 do not run (`Activation: n/a`).
- Fix-claim and checker calls use `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` (the value other projects' checkers use in this repo); `CLAUDE_FIXER_VERDICT_BOT_LOGIN` stays empty.
