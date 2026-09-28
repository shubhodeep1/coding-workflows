# Implement-Plan Log — Claude-fixer review: a failed reviewer slot is a missing vote, not a finding

- Plan: docs/plans/issue-4835-failed-reviewer-slot-missing-vote-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4835-failed-reviewer-slot-missing-vote   Final PR: (opening) draft
- Source issue: shubhodeep1/coding-workflows#4835   Base branch: main   Security pass: run
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened from main; phase 1 starting

## Phases
1. [ ] Phase 1 — Failed reviewer slots are missing votes (classifier in the Claude-fixer clean-ledger check, CLAUDE_FIXER_MIN_CLEAN_REVIEWERS, CLAUDE_FIXER_CLEAN_WITH_FAILED_SLOTS, tests, docs, changelog)
   - scripts/review_autofix_step_claude_fixer_handoff.sh: classifier replaces the clean check; failed slot = exact failure line for the block's own model + status file `failed`; clean votes need status `success` on the failed-slot path; minimum default 5
   - .github/workflows/review_autofix.yml: hand-off step env CLAUDE_FIXER_MIN_CLEAN_REVIEWERS
   - tests/test_review_autofix_claude_fixer_mode.py: 5 clean + 1 failed → clean; 4 + 2 → hand-off; 5 + 1 finding → hand-off; forged failure line → finding; status and model binding; invalid minimum
   - README.md, agents.md, changelog.d/4835-failed-reviewer-slot-missing-vote.md
   - Done: new and existing Claude-fixer tests pass, workflow size test passes, docs name the variable and log key

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Does the minimum-clean-reviewers rule apply to every ledger or only when a slot failed? — Picked: A — only when at least one verified failed slot is present; a ledger without one keeps today's every-block-clean rule. Alternatives: B — every ledger. Why: B would stall panels with fewer than 5 reviewers that merge today; the issue scopes the minimum to failed-slot ledgers. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] How is a failed slot proven? — Picked: A — the block's single line is the runner's terminal failure line for that block's own model, and status_review_<slug>.txt reads `failed`. Alternatives: B — the failure line text alone. Why: the ledger is LLM output over reviewer output, so text alone is forgeable; the status file is written by the runner only. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] Which slot outcomes count as failed? — Picked: A — the three retry-exhaustion lines. Alternatives: B — also non-retryable errors and skipped slots. Why: those are the cases the issue names, with fixed text that parses strictly; the others keep failing closed. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] Must a clean vote on the failed-slot path be backed by a `success` status file? — Picked: A — yes. Alternatives: B — the text alone. Why: the issue requires an exact, reviewer-produced empty result. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28] What does an invalid CLAUDE_FIXER_MIN_CLEAN_REVIEWERS do? — Picked: A — warn and use the default 5. Alternatives: B — hand off every failed-slot ledger. Why: 5 keeps the check strict; 0 must never make a ledger with no clean vote clean. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] Coordinate with the Claude-fixer convergence project? — Picked: A — land on main independently, confined to the clean-check block and one hand-off line. Alternatives: B — wait for that project. Why: the stall blocks projects now and the hunks do not overlap. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher; session session_01XdBB5gDqt5SfbNY5r5iMAe in Auto mode.
- Issue progress comment id 5871103278.
- security_pass_skip.py: {"skip": false, "reason": "no skip label"}.
