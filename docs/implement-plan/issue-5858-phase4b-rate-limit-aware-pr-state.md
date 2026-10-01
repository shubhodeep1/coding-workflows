# Implement-Plan Log — Release gate Phase 4b: wait out GitHub rate limits instead of treating them as an outage

- Plan: docs/plans/issue-5858-phase4b-rate-limit-aware-pr-state-plan.md
- Source issue: shubhodeep1/coding-workflows#5858
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5858-phase4b-rate-limit-aware-pr-state   Final PR: #5861 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5874
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01LmB1TgVqo23aKNYHw34Stg   safety net (re-armed by the round 5 stage, see its report)   hand-back (re-armed by the round 5 stage)
- Last updated: 2026-10-01
- Last note: review round 5 on PR #5874: both consensus findings fixed (the completed-run guard now requires a PR-state read in the same poll that confirmed the PR `open`, so an `unknown` read no longer lets a completed run skip `pr_closed_during_retry`; consecutive unknowns still trip the 4-read breaker; contract and three behavioural tests cover `unknown`; AD-12).

## Phases
1. [ ] Phase 1 — rate-limit-aware Phase 4b PR-state polling — PR #5874 open (waiting); review rounds: 5; interventions: 0 (`.github/workflows/test-and-mark-stable.yml`, `tests/test_test_and_mark_stable_review_blocked_budget.py`, `changelog.d/5858-phase4b-rate-limit-aware-pr-state.md`)
   - [x] `gh_api_with_retry` returns `GH_API_RATE_LIMITED_RC` (75) on a rate-limited attempt without the short retries
   - [x] `fetch_pr_state` reports `rate_limited` separately from `unknown`
   - [x] `phase4b_rate_limit_wait_seconds` derives the wait from `GET /rate_limit`, capped at the deadline
   - [x] poll loop: `rate_limited` branch leaves `PR_STATE_FAILURES` unchanged and waits (capped at the registration deadline while no run is registered)
   - [x] behavioural + contract tests; every existing test in the file passes under `python3 <file>` and `pytest`
   - [x] changelog fragment
   - [x] review round 1: every read in the poll loop (PR state, post-dispatch run list, pinned run status) waits out a rate limit (`phase4b_wait_out_rate_limit`)
   - [x] review round 2: a wait that slept is followed by one read before the next 15s poll sleep or deadline check (`RETRY_READ_AFTER_RATE_LIMIT_WAIT`)
   - [x] review round 3: a PR-state wait that slept is followed by a PR-state read, not a run read (`PR_STATE_RATE_LIMIT_READ_OWED`)
   - [x] review round 4: a completed run ends the loop only after a PR-state read in that poll that was not rate-limited (`RETRY_RUN_COMPLETION_ACCEPTED`)
   - [x] review round 5: that PR-state read must confirm the PR `open`; `unknown` no longer accepts a completed run
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
- AD-8 [phase 1/1 — review round 1, 2026-10-01] Should the post-dispatch run-list read wait out a rate limit, given the 90-second registration window? — Picked: A — yes, the same wait as every other poll-loop read, capped at the registration deadline. Alternatives: B — wait only on the pinned-run status read and keep re-reading the run list every 15 s; C — extend the registration window by the wait. Why: GitHub documents a minimum 60 s wait for secondary limits and re-reading a limited endpoint can prolong it; AD-6 keeps the window fixed, so a sustained limit still fails closed with `retry_dispatch_failed`. Applied in: PR #5874. Status: pending review
- AD-9 [phase 1/1 — review round 2, 2026-10-01] How should the loop make sure a rate-limit wait that ends at (or within 15s of) a deadline is followed by a read? — Picked: A — a wait that slept owes one read: the loop skips its next 15s poll sleep and lets that read through the registration-window and retry-deadline checks; a zero wait (deadline already reached) owes nothing. Alternatives: B — cap every wait 15s short of the deadline; C — reject the finding. Why: B still loses the read when the limit clears in the last 15s and shortens every wait; A adds at most one read per deadline and keeps the loop finite. Applied in: PR #5874. Status: pending review
- AD-10 [phase 1/1 — review round 3, 2026-10-01] After a rate-limited PR-state read has waited, which read comes next? — Picked: A — the PR-state read again (`continue` to the loop top), clearing the owed-read flag before the wait and, on a zero wait, restoring a read an earlier wait owed so the registration-window run-list read is never lost. Alternatives: B — keep falling through to the run read and re-check the PR state only before breaking on a completed run; C — reject the finding as unchanged from the old `unknown` handling. Why: a wait can now last up to the whole deadline, so a PR-state read taken before it is stale; A is the smallest fail-closed change and keeps one loop exit, and it departs from the plan's Approach ("falls through to the run read in the same iteration") only after a wait that slept. Applied in: PR #5874. Status: pending review
- AD-11 [phase 1/1 — review round 4, 2026-10-01] When a zero wait at a deadline falls through to the run reads and the run has completed, what ends the loop? — Picked: A — a completed run ends the loop only after a PR-state read in the same poll that was not rate-limited; otherwise the loop keeps polling, and if the deadline has passed it fails closed with `retry_timeout` (a post-loop check on `RETRY_RUN_COMPLETION_ACCEPTED`, with its own error line). Alternatives: B — end the loop at once on any zero-wait PR-state read (`retry_timeout`, or `retry_dispatch_failed` in the registration window); C — reject the finding as the plan's accepted risk. Why: B would drop the run-list read at the window end that round 3 (AD-10) kept; A keeps every earlier read guarantee, keeps the loop finite, and never proceeds to attempt 2 on a PR whose state stayed unread. Applied in: PR #5874. Status: pending review
- AD-12 [phase 1/1 — review round 5, 2026-10-01] Should an `unknown` PR-state read (a non-rate-limit failure) let a completed run end the Phase 4b wait, as it did on main before this PR? — Picked: A — no; accept a completed run only when the PR-state read in that poll returned `open`, otherwise keep polling (consecutive unknowns still trip the 4-read breaker with `pr_state_check_failed`, and a deadline still fails closed with `retry_timeout`). Alternatives: B — reject the finding as unchanged pre-PR behaviour; C — reject `unknown` only once the breaker count is above 0. Why: §1 puts correctness first and round 4 already made the guard's purpose "confirm the PR is still open"; testing for the confirming value is the smallest fail-closed change and keeps the loop finite. Applied in: PR #5874. Status: pending review

## Lessons
- [source:intervention] When a helper starts returning a distinct exit code for a failure class, audit every caller in the same loop, not only the one the incident hit: a caller that keeps treating the new code as a generic failure silently skips the new handling. (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] When a poll loop gains a variable-length wait (rate limit, backoff), make the read that follows the wait run before the loop's fixed poll sleep and its deadline checks; otherwise a wait capped at a deadline is never followed by the read it waited for. (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] A long wait inside a poll loop makes every guard checked before it stale: after the wait, go back to the loop top so the guards run again before any later read can end the loop, and clear a carried-over "read owed" flag before the wait so a zero wait cannot keep the loop alive. (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] In a poll loop that waits on two reads, let the success exit check that the guard read in the same iteration actually answered; a guard read that was rate-limited at a deadline must not let the other read end the loop. (files: .github/workflows/test-and-mark-stable.yml)
- [source:intervention] A guard that must confirm a state should test for the confirming value (`!= "open"`), not exclude the known-bad values one at a time: every new failure value (`rate_limited`, `unknown`) otherwise slips through until a reviewer finds it. (files: .github/workflows/test-and-mark-stable.yml)

## Notes
- Issue mode: started by the Claude issue dispatcher (routine "PR dispatch: #5858", trigger trig_01Mjrhnyrr1MrJ7ar8r3ViaM) in session session_01KSbUQQrXpu9Y6S3y5WG4mb, permission mode auto.
- Protected paths: none (no `.claude/**` file is touched).
- Progress comment on #5858: 5923463562.
