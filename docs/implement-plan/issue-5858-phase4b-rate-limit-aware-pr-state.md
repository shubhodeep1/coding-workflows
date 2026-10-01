# Implement-Plan Log — Release gate Phase 4b: wait out GitHub rate limits instead of treating them as an outage

- Plan: docs/plans/issue-5858-phase4b-rate-limit-aware-pr-state-plan.md
- Source issue: shubhodeep1/coding-workflows#5858
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5858-phase4b-rate-limit-aware-pr-state   Final PR: #5861 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: phase 1 implemented and verified; phase PR opened against the project branch.

## Phases
1. [ ] Phase 1 — rate-limit-aware Phase 4b PR-state polling (`.github/workflows/test-and-mark-stable.yml`, `tests/test_test_and_mark_stable_review_blocked_budget.py`, `changelog.d/5858-phase4b-rate-limit-aware-pr-state.md`)
   - [x] `gh_api_with_retry` returns `GH_API_RATE_LIMITED_RC` (75) on a rate-limited attempt without the short retries
   - [x] `fetch_pr_state` reports `rate_limited` separately from `unknown`
   - [x] `phase4b_rate_limit_wait_seconds` derives the wait from `GET /rate_limit`, capped at the deadline
   - [x] poll loop: `rate_limited` branch leaves `PR_STATE_FAILURES` unchanged and waits (capped at the registration deadline while no run is registered)
   - [x] behavioural + contract tests; every existing test in the file passes under `python3 <file>` and `pytest`
   - [x] changelog fragment
   - Done when: the plan's Phase 1 "done" condition holds.

## Conformance

## Security pass
- Skipped: `Security pass: skip (ai:workflow-heal: automation-produced issue)`, verified by `.claude/scripts/security_pass_skip.py`.

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] What should `gh_api_with_retry` do on a rate-limited attempt? — Picked: A — stop retrying and return a distinct exit code 75 that existing callers still treat as failure. Alternatives: B — keep the 3 short retries and signal only on the last; C — wait for the reset inside the helper for every caller. Why: short retries cannot succeed before the reset, and only the poll loop has a deadline to cap a wait. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Where does the wait length come from? — Picked: A — `GET /rate_limit` (`core.remaining == 0` → reset + 1 s, otherwise 60 s, unreadable → 60 s), capped at the deadline. Alternatives: B — parse `x-ratelimit-reset` via `gh api -i` on every call; C — fixed exponential backoff. Why: the endpoint is free, already used by `scripts/workflow_retro.py`, and leaves every caller's output format unchanged. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How does a rate-limited PR-state read affect the 4-failure breaker? — Picked: A — it neither increments nor resets `PR_STATE_FAILURES`. Alternatives: B — reset the counter; C — count it as before. Why: a rate limit says nothing about whether the PR is resolvable, and leaving the counter unchanged keeps the breaker as strict as today for real failures. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Which status does a rate-limited deadline expiry report? — Picked: A — the existing `retry_timeout`. Alternatives: B — a new status value. Why: the aggregator and its status list stay unchanged (§5, §6), and the step log names the rate-limit waits. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] What form does the regression test take? — Picked: A — a behavioural test that runs the step's own text in bash with a stub `gh` and a fake clock, plus string contract checks. Alternatives: B — string contract checks only. Why: the issue asks to confirm the gate keeps verifying the adopted run, which only running the code shows. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] Should a rate-limit wait extend the 90-second registration window on the dispatch path? — Picked: A — no; cap the wait at the registration deadline while no run is registered. Alternatives: B — extend the window by the wait. Why: the issue is about the adopted-run path, and capping keeps the dispatch path's timing exactly as today. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-10-01] Which docs change? — Picked: A — the step's own comments plus a changelog fragment; README.md and agents.md do not describe Phase 4b polling. Alternatives: B — also add an agents.md note. Why: §5 minimal change; the behaviour is documented where operators read it (the step log and comments). Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: started by the Claude issue dispatcher (routine "PR dispatch: #5858", trigger trig_01Mjrhnyrr1MrJ7ar8r3ViaM) in session session_01KSbUQQrXpu9Y6S3y5WG4mb, permission mode auto.
- Protected paths: none (no `.claude/**` file is touched).
- Progress comment on #5858: 5923463562.
