# Implement-Plan Log — Require a captured Plan run ID before the release smoke test reports Plan success

- Plan: docs/plans/issue-4723-require-plan-run-id-plan.md
- Source issue: shubhodeep1/coding-workflows#4723   Base branch: stable
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4723-require-plan-run-id   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened from stable; implementing phase 1

## Phases
1. [ ] Phase 1 — paginate the scoped Plan run lookup and require the ID before success (`.github/workflows/test-and-mark-stable.yml` `wait-plan` step, `tests/test_test_and_mark_stable_plan_polling_guard.py`, `changelog.d/4723-require-plan-run-id.md`)
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

## Lessons

## Notes
- Issue progress comment: 5864145856.
- Invoking session: session_01JsFVWAjMCmc5aojW1k4Am2 (started by the Claude issue dispatcher).
