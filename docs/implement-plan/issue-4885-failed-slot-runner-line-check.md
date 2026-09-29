# Implement-Plan Log — Claude-fixer review: prove a failed reviewer slot from the runner's own output line

- Plan: docs/plans/issue-4885-failed-slot-runner-line-check-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4885-failed-slot-runner-line-check   Final PR: #4918 draft
- Source issue: shubhodeep1/coding-workflows#4885   Base branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Security pass: skip (ai:security: automation-produced issue)
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4983
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_0194bHyqD8cWSnZbw18P8kbx (project checker, reused)
- Last updated: 2026-09-29
- Last note: review round 2 on PR #4983: the one consensus task gap fixed (the changelog fragment now names all four runner-file warning states: missing, empty, unreadable, different), 0 rejected; 117 Claude-fixer and changelog tests pass

## Phases
1. [ ] Phase 1 — Bind failed reviewer slots to the runner's output line
   - scripts/review_autofix_step_claude_fixer_handoff.sh: awk prints the failed block's line; duplicate check extracts slugs per kind; failed:failed requires review_<slug>.txt to equal the line, else warn and fail closed
   - tests/test_review_autofix_claude_fixer_mode.py: runner output files in _run_handoff/_panel; new tests for a non-retryable runner line, a missing runner file, an extra runner line, and a different retry-exhaustion variant
   - README.md, agents.md, changelog.d/4885-failed-slot-runner-line-check.md
   - Done: new and existing Claude-fixer tests pass, bash -n and shellcheck clean, docs describe the runner-line match
   - PR #4983 open; review rounds: 2 (2026-09-29: round 1 — 2 consensus findings fixed, 0 rejected; round 2 — 1 consensus task gap fixed, 0 rejected); interventions: 0

## Conformance

## Security pass
- Skipped: ai:security: automation-produced issue (security_pass_skip.py)

## Validation

## Completion

## Activation
- n/a (base claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote)

## Auto-decisions
- AD-1 [plan, 2026-09-29] How is a failed slot's failure class proven? — Picked: A — require the runner-written `review_<slug>.txt` to equal the ledger block's line exactly. Alternatives: B — add runner-produced failure-class metadata (new status value or file) in `review_run_reviewers.sh`. Why: the runner already writes the exact class-bearing line for every terminal failure, so A closes the gap with no runner change and no change for other readers of `status_review_*.txt` (§5); the issue lists it first. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] Where does the changelog entry go, given #4835 has not reached `main` yet? — Picked: A — a new fragment `changelog.d/4885-failed-slot-runner-line-check.md` under `security`. Alternatives: B — edit `changelog.d/4835-failed-reviewer-slot-missing-vote.md`. Why: §20.B is one fragment per PR and never another PR's file. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] Also cross-check clean blocks against the runner output? — Picked: A — no, failed blocks only. Alternatives: B — also require a clean block's runner output to be an empty review. Why: the finding and its recommendation are about failed blocks; clean votes already need a runner `success` status, and reviewer output formats vary (§5). Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] When a fail-closed check reads a file and reports why it rejected it, tell missing, unreadable, empty, and different apart: `$(cat f 2>/dev/null || true)` makes an unreadable file look empty, and an empty file look like a content mismatch. (files: scripts/review_autofix_step_claude_fixer_handoff.sh)
- [source:intervention] When a review-round fix adds a new state to a log or warning message, update every doc that quotes that message (the changelog fragment's contributor section in particular) in the same commit; reviewers flag the stale quote as a task gap in the next round. (files: changelog.d/4885-failed-slot-runner-line-check.md)

## Notes
- Final PR #4918 opened as a draft into the base branch on 2026-09-29.
- The step 3a log commit (6a15ce3) was authored with an overridden git identity by mistake; later commits use the session's configured identity. The project branch is never force-pushed, so it stays.
- Base branch check (2026-09-29): PR #4847 (head = the base branch) is an open draft into main, so the base has not moved.
- security_pass_skip.py: {"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}.
- This session had no mcp__github__* tools; GitHub reads and routine writes went through gh api (REST).
