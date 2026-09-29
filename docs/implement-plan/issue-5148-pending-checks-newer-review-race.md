# Implement-Plan Log — Pending-checks auto-merge: defer while a newer review of the PR is active

- Plan: docs/plans/issue-5148-pending-checks-newer-review-race-plan.md
- Source issue: shubhodeep1/coding-workflows#5148
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
- Project branch: claude/implement-plan-issue-5148-pending-checks-newer-review-race   Final PR: #5178 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR from `claude/implement-plan-issue-5148-pending-checks-newer-review-race-phase-1` (its number is in the checker's resume block and the stage report)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: project checker armed after the phase 1 PR opened (ids in the stage report and the resume block)
- Last updated: 2026-09-29
- Last note: phase 1 implemented and verified: `check_review_runs()` defers the pending-checks merge (`review_active` / `review_superseded`); 94 tests in `tests/test_claude_fixer_pending_checks.py` pass, 14 new cases fail on the unfixed evaluator.

## Phases
1. [ ] Phase 1 — defer the pending-checks merge while a newer review is active or unsettled   — PR open (waiting); review rounds: 0; interventions: 0
   - `check_review_runs()` in `scripts/claude_fixer_pending_checks.py`: head-branch runs, `[pr:<N>]` internal-review dispatches, unbound `review_autofix.yml` / `ai-review.yml` dispatches
   - `evaluate()` returns `review_active` / `review_superseded` and re-reads the marker before merging
   - Tests: the audit's exploit scenario and every defer / supersede path; existing #4900 suites green
   - `README.md`, `agents.md`, `docs/INVENTORY.md`; `changelog.d/5148-pending-checks-newer-review-race.md`
   - Done: the plan's phase 1 "done" condition. Protected paths: none.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which review runs count as a newer review of the PR? — Picked: A — every run on the PR's head branch, `internal-review.yml` dispatches bound by the `[pr:<N>]` title, and (fail closed) any active `review_autofix.yml` / `ai-review.yml` dispatch. Alternatives: B — title-bound dispatches only; C — any active workflow run in the repository. Why: covers every path the gate lets a forced review take. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What does "authorize only against the latest completed review" mean? — Picked: A — the latest completed review bound to this PR and newer than the marker's run must have concluded `success`, and the same marker must still be live on a re-read right before the merge. Alternatives: B — the marker's run must be the latest completed review; C — active runs only. Why: gate-skipped sweep dispatches conclude `success` and must not block; a failed newer review left no verdict. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which statuses count as active? — Picked: A — any status other than `completed`. Alternatives: B — `queued` / `in_progress` / `pending` only. Why: fail closed (§1). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Reuse `check_in_status._active_run_count` or read the listings directly? — Picked: A — one head-branch listing plus one dispatch listing per review workflow, reusing `check_in_status`'s constants. Alternatives: B — `_active_run_count` plus completed-run reads. Why: fewer calls (§15). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Do the new states change the sweep's counters? — Picked: A — no; logged by the existing `pending_checks` line. Alternatives: B — count `review_active` as waiting. Why: §5. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-29] Where does the fix live? — Picked: A — in `evaluate()`. Alternatives: B — in the sweep loop. Why: the module owns the merge decision and its tests. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:security] Before an automated job enables auto-merge from an earlier review's result, it must check for newer reviews of the same PR that are still running (including dispatches from the default branch, whose check runs never attach to the PR head) and re-read the result after that check; nothing disables auto-merge once a later review finds a problem. (files: scripts/claude_fixer_pending_checks.py)

## Notes
- Issue-mode project started by the Claude issue dispatcher routine (trigger trig_01REnV4f26hDKipeNXXCHKgR) in session session_01AnxtvQ843pzNkV2iTQRcJc (Auto mode). Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5148#issuecomment-5891537425
- Security pass: skip (`security_pass_skip.py`: `ai:security` created and labelled by the issue automation).
- Base branch is the #4900 project branch (final PR #4922, open at start). The final PR targets it, so the final-merge stage closes #5148 explicitly and activation is n/a.
- Stale Routine sweep (CLAUDE.md §26.G) at start: `list_triggers` was refused by the Auto-mode classifier; skipped, not blocking.
- Plan deviation (wording only): completed `internal-review.yml` dispatches are bound to the PR by their `[pr:<N>]` title alone, like `check_in_status._active_run_count`, instead of `_is_pr_dispatched_review_run`; a broader match can only block a merge. The plan text was updated in the phase 1 PR.
- Local verification ran on Python 3.11; `tests/test_workflow_retro.py` cannot import `scripts/workflow_retro.py` there (3.12 f-string syntax), unrelated to this project. CI runs 3.12.
