# Implement-Plan Log — Claude-fixer review: prove a failed reviewer slot from the runner's own output line

- Plan: docs/completed/issue-4885-failed-slot-runner-line-check-plan.md (was docs/plans/issue-4885-failed-slot-runner-line-check-plan.md)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4885-failed-slot-runner-line-check   Final PR: #4918 draft
- Source issue: shubhodeep1/coding-workflows#4885   Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Security pass: skip (ai:security: automation-produced issue)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote)
- Waiting on: completion PR (claude/implement-plan-issue-4885-failed-slot-runner-line-check-complete)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_0194bHyqD8cWSnZbw18P8kbx (project checker, reused)
- Last updated: 2026-09-29
- Last note: Q1 answered A on #4885 (standing decision Q17): validation skipped, covered by the #4835 project's validation; completion PR moves the plan to docs/completed/, then the final merge of #4918 into the #4835 project branch.

## Phases
1. [x] Phase 1 — Bind failed reviewer slots to the runner's output line
   - scripts/review_autofix_step_claude_fixer_handoff.sh: awk prints the failed block's line; duplicate check extracts slugs per kind; failed:failed requires review_<slug>.txt to equal the line, else warn and fail closed
   - tests/test_review_autofix_claude_fixer_mode.py: runner output files in _run_handoff/_panel; new tests for a non-retryable runner line, a missing runner file, an extra runner line, and a different retry-exhaustion variant
   - README.md, agents.md, changelog.d/4885-failed-slot-runner-line-check.md
   - Done: new and existing Claude-fixer tests pass, bash -n and shellcheck clean, docs describe the runner-line match
   - PR #4983 merged 2026-09-29 (963cb35); review rounds: 2 (2026-09-29: round 1 — 2 consensus findings fixed, 0 rejected; round 2 — 1 consensus task gap fixed, 0 rejected); interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security). Implemented: COMPLETE (every goal and implementation step maps to `scripts/review_autofix_step_claude_fixer_handoff.sh:123-257`, the new tests in `tests/test_review_autofix_claude_fixer_mode.py`, README.md, agents.md, and the changelog fragment). Correctness: PASS: only `scripts/review_run_reviewers.sh` writes the failure lines (`:4443-4447`, `:4698`, `:4710-4712`, `:4728-4730`), the summariser's slug is the runner's `safe_name` (`scripts/summarize_reviewer_consensus.sh:124-125`), and resume reuses only `success` slots (`scripts/review_run_reviewers.sh:248-256`). Checks: bash -n, shellcheck, the Claude-fixer and file-size tests (72 passed, mawk), the Claude-fixer tests under gawk 5.2.1 (70 passed), and all 15 `tests/test_review_autofix_*.py` files (324 passed).

## Security pass
- Skipped: ai:security: automation-produced issue (security_pass_skip.py)

## Validation
- Cycle 1 — 2026-09-29: not dispatched. `validate.yml` ("Authorize explicit validation target") accepts `target_ref` only when exactly one open PR has that head and `base=<default branch>`. Final PR #4918 targets `claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote`, so the same listing returns 0 PRs and the run would fail before validating. Validating the default branch or the base branch in its place is not allowed, so this is a stop (CLAUDE.md §28.C), asked on #4885.
- Validation: skipped (covered by #4835's project validation). Q1: A answered on #4885 (comment 5885304060, 2026-09-29) under the master session's standing decision Q17; the #4835 project runs its security audit and runtime validation on a branch carrying this change before anything reaches `main`. Long-term fix: #4734.

## Completion
- Completion PR (branch claude/implement-plan-issue-4885-failed-slot-runner-line-check-complete) opened 2026-09-29 — doc moved to docs/completed/issue-4885-failed-slot-runner-line-check-plan.md
- Final PR #4918 draft (into the #4835 project branch)

## Activation
- n/a (base claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote)

## Auto-decisions
- AD-1 [plan, 2026-09-29] How is a failed slot's failure class proven? — Picked: A — require the runner-written `review_<slug>.txt` to equal the ledger block's line exactly. Alternatives: B — add runner-produced failure-class metadata (new status value or file) in `review_run_reviewers.sh`. Why: the runner already writes the exact class-bearing line for every terminal failure, so A closes the gap with no runner change and no change for other readers of `status_review_*.txt` (§5); the issue lists it first. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Where does the changelog entry go, given #4835 has not reached `main` yet? — Picked: A — a new fragment `changelog.d/4885-failed-slot-runner-line-check.md` under `security`. Alternatives: B — edit `changelog.d/4835-failed-reviewer-slot-missing-vote.md`. Why: §20.B is one fragment per PR and never another PR's file. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Also cross-check clean blocks against the runner output? — Picked: A — no, failed blocks only. Alternatives: B — also require a clean block's runner output to be an empty review. Why: the finding and its recommendation are about failed blocks; clean votes already need a runner `success` status, and reviewer output formats vary (§5). Applied in: no code change. Status: pending review
- AD-4 [validation 1/3, 2026-09-29] How is the BLOCKED state persisted when no PR is in flight? — Picked: A — one docs-only log commit pushed straight to the project branch, like the step 3a log commit. Alternatives: B — leave the log at IN_PROGRESS and carry the state only in the issue comment. Why: with B, `/implement-issue-claude` step 4 sees a live checker and `Status: IN_PROGRESS` and stops as "already in progress", so a `/reclarify` could not resume the project. Applied in: no code change (log commit). Status: pending review

## Lessons
- [source:plan-deviation] An issue-mode project whose base is not the default branch cannot pass runtime validation with `target_ref`: `validate.yml` authorizes an explicit target only through an open PR from that branch into the default branch, so check the final PR's base before dispatching step 10. (files: .github/workflows/validate.yml, .claude/commands/implement-plan-claude.md)
- [source:intervention] When a fail-closed check reads a file and reports why it rejected it, tell missing, unreadable, empty, and different apart: `$(cat f 2>/dev/null || true)` makes an unreadable file look empty, and an empty file look like a content mismatch. (files: scripts/review_autofix_step_claude_fixer_handoff.sh)
- [source:intervention] When a review-round fix adds a new state to a log or warning message, update every doc that quotes that message (the changelog fragment's contributor section in particular) in the same commit; reviewers flag the stale quote as a task gap in the next round. (files: changelog.d/4885-failed-slot-runner-line-check.md)

## Notes
- Final PR #4918 opened as a draft into the base branch on 2026-09-29.
- The step 3a log commit (6a15ce3) was authored with an overridden git identity by mistake; later commits use the session's configured identity. The project branch is never force-pushed, so it stays.
- Base branch check (2026-09-29): PR #4847 (head = the base branch) is an open draft into main, so the base has not moved.
- security_pass_skip.py: {"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}.
- This session had no mcp__github__* tools; GitHub reads and routine writes went through gh api (REST).
- 2026-09-29: blocked at validation (comment 5885103667, `ai:claude-blocked`); answered Q1: A by the repo owner (comment 5885304060, standing decision Q17 in docs/operations/master-session.md); label removed and the stage continued in the same session.
