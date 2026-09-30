# Implement-Plan Log — Merge-train release: match `@ref`-suffixed run paths and read every active run before releasing a queued PR

- Plan: docs/completed/issue-5443-merge-train-complete-run-listing-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5443
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5443-merge-train-complete-run-listing   Final PR: #5451 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a once the final PR merges (base claude/implement-plan-issue-4701-review-dispatch-default-branch is not the default branch)
- Waiting on: completion PR (the PR carrying this log update)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_018ZxuZKFeRNRTtGvjHSjwZa (project checker)   safety net and hand-back: see the validation 1/3 — read result stage report
- Last updated: 2026-09-30
- Last note: validation cycle 1 passed on the project branch at 7cef44d (10/10 tests); completion PR moves the plan to docs/completed/; final PR #5451 is marked ready once it merges.

## Phases
1. [x] Phase 1 — complete, suffix-tolerant active-run listing for the merge-train release   — PR #5458 merged 2026-09-30; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-30: INCOMPLETE — fix PR #5512 (pre-security). Correctness FAIL: `scripts/review_merge_train.sh:486` compared the runs read with the latest page's total_count, so a status-filtered listing that shrank between pages (runs finishing) skipped an active review run and released beside it; fixed by treating any total_count change after page 1 as listing_shifted (AD-7). Not fixed: the same path regex in `scripts/gh_helpers.sh:1264`, `:1388` (plan non-goal, AD-5). PR #5512 review rounds: 1 (round 1, 2026-09-30: every reviewer flagged that a same-count membership shift still skipped a run under AD-7's check; valid, fixed by AD-8's created_at-bounded queries, which replace the offset pages and AD-7's page-1 total_count comparison).
- Run 2 — 2026-09-30: CONFORMANT — no fixes (pre-security). Correctness CONCERNS: one HYPOTHESIS, not verifiable live: the keyset assumes a re-run attempt keeps its created_at sort position (no re-run appeared in the repo's recent listings).

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation
- Cycle 1 — run 36714864057 2026-09-30 (target_ref: project branch, 7cef44d): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 285s); no fixes

## Completion
- Completion PR (this log update) — doc moved to docs/completed/issue-5443-merge-train-complete-run-listing-plan.md
- Merged PRs: phase 1 #5458, conformance fix #5512
- Final PR #5451 draft (project mode, base claude/implement-plan-issue-4701-review-dispatch-default-branch; marked ready in the final-merge stage)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should the active-run listing become complete? — Picked: A — three status-filtered repo-wide listings (`pending`, `queued`, `in_progress`), paged to `total_count`, at most 10 pages each. Alternatives: B — paginate the unfiltered `actions/runs` listing; C — one listing per review workflow per status (9 calls). Why: active runs are few, `total_count` proves completeness, and it keeps the call count at 3. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What does `release` do when the listing is incomplete? — Picked: A — leave every queued PR queued this invocation and retry on the next close event or poll tick. Alternatives: B — keep releasing without the guard (today's behaviour). Why: the file's fail-open contract already says release does nothing on API failure, and releasing blind is the defect. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] How is the `@<ref>` suffix handled? — Picked: A — strip it with `sub("@.*$"; "")` before the existing regex. Alternatives: B — extend the regex with `(@.*)?$`. Why: one transform keeps the regex identical to the other matchers in the repo. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] When is the listing fetched? — Picked: A — lazily, once, on the first queued PR that passes the base filter. Alternatives: B — eagerly before the loop, as today. Why: §15, the new listing costs 3 calls, and most ticks have no queued PR. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Fix the same path regex in `scripts/gh_helpers.sh` too? — Picked: A — no, record it for a separate issue. Alternatives: B — include both helpers in this PR. Why: §5; the finding names the merge train and those helpers belong to a different flow with their own tests. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Which statuses count as active? — Picked: A — keep `pending`, `queued`, `in_progress`, the three the filter already uses. Alternatives: B — also `waiting` and `requested` (5 calls). Why: §5, no semantic widening; the review workflows use no deployment environments that would hold a run in `waiting`. Applied in: phase 1. Status: pending review
- AD-7 [conformance 1/3, 2026-09-30] How should the active-run listing treat a `total_count` that changes between pages of one status? — Picked: A — mark the listing incomplete (`listing_shifted`) when any later page's `total_count` differs from page 1's. Alternatives: B — compare the runs read with page 1's `total_count` only; C — compare with the largest `total_count` seen. Why: §1/§28.B, the safer option: A keeps every listing that is incomplete today incomplete and adds the shrink case; B would newly accept a listing that grew mid-read, and C still accepts a shrink that later additions offset. Applied in: PR #5512. Status: pending review
- AD-8 [conformance 1/3 — review round, 2026-09-30] How should the active-run listing read a status that holds more than 100 runs, now that a same-count membership shift defeats AD-7's total_count check? — Picked: A — replace offset pages with keyset queries: each follow-up query is `status=<s>&created=<=<oldest created_at read>` (inclusive, deduplicated by id), a status is complete when one response holds its whole `total_count`, at most 10 queries, and a query that adds no new run is `truncated`. Alternatives: B — keep offset pages and re-read pages 1..n-1 after the last page, requiring identical ids (n-1 extra calls, still defeated by churn that restores a page); C — treat any status needing more than one page as incomplete (holds the whole train whenever a status has more than 100 active runs). Why: §1/§28.B, A is the only option that provably returns every run staying in its status during the read (the listing is sorted by created_at, newest first, checked live 2026-09-30), at the same call count as offset paging; it supersedes AD-7's comparison, which has no meaning across differently bounded queries. Applied in: PR #5512. Status: pending review

## Lessons
- [source:plan-deviation] Fake `gh` fixtures for paged `actions/runs` listings need a distinct `id` per run: a listing deduplicated by id collapses id-less runs and reads as incomplete. (files: tests/test_review_merge_train.py)
- [source:conformance] Offset pages over a status-filtered GitHub run listing can skip a run that stays active, even when total_count never changes (one run leaves above it while another enters below the page boundary), so page with a keyset instead: bound each follow-up query by `created=<=<oldest created_at read>` and count a status complete only when one response holds its whole total_count. A time-windowed listing with no status filter only grows at the top and does not need this. (files: scripts/review_merge_train.sh, tests/test_review_merge_train.py)
- [source:intervention] Before accepting a paging-completeness fix, test the membership swap that keeps total_count unchanged (one run leaves, one enters mid-list): count checks alone never detect it. (files: tests/test_review_merge_train.py)

## Notes
- Issue mode: the base branch is the #4701 project branch (final PR #4709, open draft into main on 2026-09-30), not the default branch, so the final PR uses `Refs #5443` and the final-merge stage closes the issue explicitly; activation is n/a for this base.
- Issue progress comment: 5904107812.
