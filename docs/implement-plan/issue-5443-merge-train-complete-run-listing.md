# Implement-Plan Log — Merge-train release: match `@ref`-suffixed run paths and read every active run before releasing a queued PR

- Plan: docs/plans/issue-5443-merge-train-complete-run-listing-plan.md
- Source issue: shubhodeep1/coding-workflows#5443
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5443-merge-train-complete-run-listing   Final PR: #5451 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (claude/implement-plan-issue-5443-merge-train-complete-run-listing-phase-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented and verified (tests/test_review_merge_train.py 33 passed, tests/test_review_dispatch_default_branch.py passed, shellcheck --severity=error clean, live read-only listing rc=0); phase PR opened into the project branch.

## Phases
1. [ ] Phase 1 — complete, suffix-tolerant active-run listing for the merge-train release   — PR open (waiting); review rounds: 0; interventions: 0

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion
- Final PR #5451 draft (project mode, base claude/implement-plan-issue-4701-review-dispatch-default-branch)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should the active-run listing become complete? — Picked: A — three status-filtered repo-wide listings (`pending`, `queued`, `in_progress`), paged to `total_count`, at most 10 pages each. Alternatives: B — paginate the unfiltered `actions/runs` listing; C — one listing per review workflow per status (9 calls). Why: active runs are few, `total_count` proves completeness, and it keeps the call count at 3. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What does `release` do when the listing is incomplete? — Picked: A — leave every queued PR queued this invocation and retry on the next close event or poll tick. Alternatives: B — keep releasing without the guard (today's behaviour). Why: the file's fail-open contract already says release does nothing on API failure, and releasing blind is the defect. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] How is the `@<ref>` suffix handled? — Picked: A — strip it with `sub("@.*$"; "")` before the existing regex. Alternatives: B — extend the regex with `(@.*)?$`. Why: one transform keeps the regex identical to the other matchers in the repo. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] When is the listing fetched? — Picked: A — lazily, once, on the first queued PR that passes the base filter. Alternatives: B — eagerly before the loop, as today. Why: §15, the new listing costs 3 calls, and most ticks have no queued PR. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Fix the same path regex in `scripts/gh_helpers.sh` too? — Picked: A — no, record it for a separate issue. Alternatives: B — include both helpers in this PR. Why: §5; the finding names the merge train and those helpers belong to a different flow with their own tests. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Which statuses count as active? — Picked: A — keep `pending`, `queued`, `in_progress`, the three the filter already uses. Alternatives: B — also `waiting` and `requested` (5 calls). Why: §5, no semantic widening; the review workflows use no deployment environments that would hold a run in `waiting`. Applied in: phase 1. Status: pending review

## Lessons
- [source:plan-deviation] Fake `gh` fixtures for paged `actions/runs` listings need a distinct `id` per run: a listing deduplicated by id collapses id-less runs and reads as incomplete. (files: tests/test_review_merge_train.py)

## Notes
- Issue mode: the base branch is the #4701 project branch (final PR #4709, open draft into main on 2026-09-30), not the default branch, so the final PR uses `Refs #5443` and the final-merge stage closes the issue explicitly; activation is n/a for this base.
- Issue progress comment: 5904107812.
