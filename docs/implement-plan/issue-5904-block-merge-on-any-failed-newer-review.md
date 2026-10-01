# Implement-Plan Log — Pending-checks auto-merge: any failed newer review blocks the merge

- Plan: docs/plans/issue-5904-block-merge-on-any-failed-newer-review-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5904-block-merge-on-any-failed-newer-review   Final PR: #5914 draft
- Source issue: shubhodeep1/coding-workflows#5904   Issue base: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5948
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01A8ebmf2eSjSX7B7z5rAFxs   safety net and hand-back re-armed by each stage (ids in the stage report)
- Last updated: 2026-10-01
- Last note: review round 1 on PR #5948 had 0 findings; the hand-off came from the empty check-run snapshot of #5880, so this log update is the new head that starts round 2 (AD-5)

## Phases
1. [ ] Phase 1 — any unsuccessful newer bound review blocks the pending-checks merge   — PR #5948 open (waiting); review rounds: 1; interventions: 0

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
- AD-5 [phase 1/1 — review round, 2026-10-01] PR #5948 review round 1 (head 8745290, run 36831472622) handed off with 0 findings from 6 successful reviewers, because the post-review check snapshot read `ready` with 0 check runs (#5880), and `CLAUDE_FIXER_VERDICT_BOT_LOGIN` is unset. How does the round close? — Picked: A — push a new head carrying this required progress-log update (PR number, round count, this entry), which starts review round 2. Alternatives: B — stop BLOCKED until a verdict bot exists or a human merges; C — add the `force-review` label and re-dispatch review on the same head. Why: there is nothing to fix or reject, main, the issue base, the project branch and the phase branch are all in sync so no base merge is due, and AD-12 / AD-18 of issue-4886 closed the same state with a new head. Applied in: PR #5948 (log commit). Status: pending review

## Lessons
- [source:intervention] A Claude-fixer hand-off with 0 ledger entries and `no fresh ready same-head check-run snapshot` in the hand-off step is the collector's empty-snapshot failure (#5880), not a review result: check the snapshot's sha256 against a local collector run before judging findings, and close the round with a new head rather than a verdict when no verdict bot is configured. (files: scripts/collect_pr_check_runs_context.py, scripts/review_autofix_step_claude_fixer_handoff.sh)

## Notes
- Issue mode (CLAUDE.md §28.A). Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5904#issuecomment-5925634297
- The issue base is not the default branch, so the final-merge stage closes #5904 explicitly and adds `ai:merged`; steps 12–13 do not run (`Activation: n/a`).
- Fix-claim and checker calls use `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN=shubhodeep1` (the value other projects' checkers use in this repo); `CLAUDE_FIXER_VERDICT_BOT_LOGIN` stays empty.
- Review round 1 (2026-10-01, head 8745290, run 36831472622, ledger d2beb473…): 0 entries from 6 successful reviewers; Copilot also reported no findings. The hand-off step logged `Claude-fixer clean review has no fresh ready same-head check-run snapshot; auto-merge disabled.` after a ~600 s collector step; its 219-byte snapshot (sha256 1e49b64e…) matches `collection_status: ready`, `total_check_runs: 0`, while the same collector run locally afterwards counts 4 runs on that head. This is #5880. A sweep-dispatched review run (36838213179) was skipped by the Claude-fixer gate (`should_run=false`). 142 pending-checks tests pass on the head. Head claimed (comment 5928012390); round closed per AD-5.
