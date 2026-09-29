# Implement-Plan Log — Page through the review wrappers' dispatch runs before a review dispatch or an empty-commit push

- Plan: docs/plans/issue-4927-paginate-review-dispatch-runs-plan.md
- Source issue: shubhodeep1/coding-workflows#4927 (https://github.com/shubhodeep1/coding-workflows/issues/4927)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4927-paginate-review-dispatch-runs   Final PR: (opened next) draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from claude/implement-plan-issue-4701-review-dispatch-default-branch (issue base); phase 1 starting

## Phases
1. [ ] Phase 1 — paginated, completeness-aware PR-named review dispatch lookup (`scripts/orchestrate_poll_process.sh` helper + 3 call sites, tests, agents.md, README.md, changelog)

## Conformance

## Security pass
- Skipped: plan header `Security pass: skip (ai:security: automation-produced issue)` (`security_pass_skip.py` verified)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which listing should replace the global page? — Picked: A — per-wrapper REST listing of `internal-review.yml` and `ai-review.yml` `workflow_dispatch` runs, paginated. Alternatives: B — repo-wide `actions/runs?event=workflow_dispatch` paginated; C — keep one page and raise `--limit`. Why: only a per-wrapper listing cannot be crowded by unrelated dispatches, as the finding recommends. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How far back should the listing reach? — Picked: A — `REVIEW_RUN_MAX_RUNTIME_MINUTES` (default 250), the poller's own review-run freshness window. Alternatives: B — 24 hours (about 8 pages here, near the 1,000 cap); C — no window (unbounded pages). Why: runs older than the window are zombies by the poller's definition, and 250 minutes is about 2 pages here. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] What counts as incomplete, and what do callers do? — Picked: A — any non-404 page failure, malformed page, cutoff failure, truncation, or shifted listing is incomplete, and every caller skips its dispatch or push this cycle. Alternatives: B — only truncation is incomplete, and API errors keep failing open. Why: a failed listing cannot prove no review is running, and the recommendation says to skip and retry. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How is "incomplete" signalled? — Picked: A — the helper keeps its stdout contract and returns 1; the direct guard prints the `listing-incomplete` sentinel; the skip paths reuse `retrigger_review_skipped_inflight`. Alternatives: B — a second helper beside the old one. Why: §6 and §5, one listing and no new state values. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Should the merge train's and the sweep's run snapshots change too? — Picked: A — no; poller only, recorded as a non-goal. Alternatives: B — also page the merge train's `_mt_inflight_review_branches`. Why: §5; the finding names the poller's empty-commit push, which neither of the others performs. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-29] How is a 404 for a wrapper treated? — Picked: A — the wrapper is absent, so its listing is complete and empty. Alternatives: B — incomplete. Why: every repo lacks one of the two wrappers, so B would block every dispatch and push forever. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue base `claude/implement-plan-issue-4701-review-dispatch-default-branch` is the head of open draft PR #4709 (into `claude/implement-plan-issue-4618-sweep-dispatch-default-branch`); checked 2026-09-29, not merged, so no base move.
- The session that started this project had no `mcp__github__*` tools; GitHub writes use `gh api` REST through the session proxy instead.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4927#issuecomment-5882676798
