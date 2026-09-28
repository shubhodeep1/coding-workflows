# Implement-Plan Log — Require a captured Plan run ID before the release smoke test reports Plan success

- Plan: docs/plans/issue-4723-require-plan-run-id-plan.md
- Source issue: shubhodeep1/coding-workflows#4723   Base branch: stable
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4723-require-plan-run-id   Final PR: #4729 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4730
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01SN9XdEWdK9hLtuWFjcG31M (hand-back and safety net re-armed each stage)
- Last updated: 2026-09-28
- Last note: review round 2 on PR #4730: 1 finding (the other-active Plan run check read only page 1) fixed per AD-6; the check now pages like the capture

## Phases
1. [ ] Phase 1 — paginate the scoped Plan run lookup and require the ID before success (`.github/workflows/test-and-mark-stable.yml` `wait-plan` step, `tests/test_test_and_mark_stable_plan_polling_guard.py`, `changelog.d/4723-require-plan-run-id.md`)
   - PR #4730 open (waiting); review rounds: 2; interventions: 0
   - Done when: new behavioural tests (missing ID fails at capture with `status=run_id_missing`; page-2 match succeeds; unrelated title rejected) and existing guard tests pass; workflow YAML parses.

## Conformance

## Security pass
- Skipped (ai:workflow-heal: automation-produced issue, verified by `.claude/scripts/security_pass_skip.py`)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Scope: fix only `wait-plan`, or also Clarify/Implement run-ID capture? — Picked: A — Plan only, as the issue specifies. Alternatives: B — also harden `wait-clarify` and `wait-implement`. Why: §5 minimal change; the evidence implicates only Plan. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How to narrow or paginate the lookup? — Picked: A — bounded page walk (≤10 pages) of the existing scoped query, stopping at the first page with a match or a short page. Alternatives: B — per-workflow run list; C — keep one page and only fail earlier. Why: B needs a repo-specific workflow file and still overflows on skipped `issue_comment` runs; C turns one false block into another. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] What does `wait-plan` report when no ID is found? — Picked: A — new status value `run_id_missing`, `::error::`, exit 1. Alternatives: B — reuse `status=plan_failed`. Why: the gate prints the status verbatim, so a distinct value names the cause. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Make the page cap configurable? — Picked: A — step-local constant `PLAN_RUN_LOOKUP_MAX_PAGES=10`. Alternatives: B — a new env var defaulting to 10. Why: §5; 10 pages is GitHub's 1,000-result ceiling for this list. Applied in: phase 1 PR. Status: pending review
- AD-5 [phase 1/1, 2026-09-28] Should the 10-second status poll page through runs too? — Picked: A — no, the poll keeps reading one page (`latest_scoped_run_field "Plan" "status" 1`), and only the run-ID capture at the success exits pages. Alternatives: B — the poll pages like the capture (up to 10 calls per poll while no run matches, about 3,600 calls an hour); C — the poll reads 2 pages. Why: §15, since the poll is an activity signal and its per-poll cost stays one call as before. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1 — review round 2, 2026-09-28] Should the "other active Plan runs" check in `wait-plan` page past the first 100 runs, although the plan listed it as a Non-goal? — Picked: A — yes: when page 1 finds no active run, walk later pages of the same query with the same shape guard and `PLAN_RUN_LOOKUP_MAX_PAGES` cap, stopping at the first page with an active run or a short page. Alternatives: B — reject the review finding as out of scope. Why: an older, still-active real Plan run behind 100 newer runs is the #4723 false release block in the same step; the branch is rare (Plan completed without labels), so the extra reads stay bounded. Applied in: phase 1 PR (review round 2). Status: pending review

## Lessons
- [source:plan-deviation] A poll loop that shares a lookup helper with a one-shot capture must not inherit the capture's pagination: pass a page cap so the per-poll API cost stays one call (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] A retry loop around a paginated lookup must not retry a walk that already read every page up to the cap: the result cannot change and each retry repeats the full page cost (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] When a lookup is fixed to page past the first 100 runs, every other check in the same step that reads the same run window needs the same paging, or the fix leaves the same false failure one branch away (files: .github/workflows/test-and-mark-stable.yml)

## Notes
- Issue progress comment: 5864145856.
- Invoking session: session_01JsFVWAjMCmc5aojW1k4Am2 (started by the Claude issue dispatcher).
