# Release gate Phase 4b: wait out GitHub rate limits instead of treating them as an outage

Source issue: shubhodeep1/coding-workflows#5858 (https://github.com/shubhodeep1/coding-workflows/issues/5858)
Base branch: main
Security pass: skip (ai:workflow-heal: automation-produced issue)

## Summary

The stable release gate (`.github/workflows/test-and-mark-stable.yml`, step `Phase 4b: Verify editor restored canary (pytest + retry)`) failed with `pr_state_check_failed` in run 36797692597. Four PR-state reads in a row were rate-limited, and the loop treated them as a GitHub outage while the adopted review run was still going. This plan makes Phase 4b recognise a rate-limited read, wait for the limit to reset inside the existing 25-minute retry deadline, and keep failing closed when that deadline expires.

## Context

- Run 36797692597 (2026-10-01): attempt 1 of the canary pytest failed as designed, and the step adopted active review run #36799926992 at 01:42:18. From 02:00:18 every `repos/<repo>/pulls/<n>` read returned `gh: API rate limit exceeded for user ID 11442166 (HTTP 403)`. `gh_api_with_retry` spent about 21 s per read on its 2 s / 4 s retries, `fetch_pr_state` returned `unknown`, and the fourth consecutive `unknown` ended the step at 02:02:06 with `PR state unresolvable after 4 consecutive attempts (likely GitHub API outage)`. That was about 5 minutes before the retry deadline (02:07:18). The run read in the same loop was still answering (`status=in_progress`), so the gate gave up while the work it waited for was still moving.
- `gh_api_with_retry` (`.github/workflows/test-and-mark-stable.yml:2143-2173`) retries every failure the same way. `fetch_pr_state` (`:2229-2236`) folds every failure into `unknown`. The poll loop (`:2419-2446`) counts each `unknown` toward `PR_STATE_FAILURE_LIMIT=4`.
- Elsewhere in this workflow, `scripts/comprehensive_test_and_release_gh_api.sh` (`gh_api_safe`) already treats a `rate limit` stderr match as its own case, and `scripts/workflow_retro.py` (`_gh_rate_limit_wait_seconds`) waits until `GET /rate_limit`'s reset epoch plus one second. This plan reuses both conventions inside Phase 4b.
- The issue's fix constraints: minimal and backward compatible, no renamed identifiers, env vars, labels, or log prefixes (CLAUDE.md §6), and no weakened check or guard.

## Goals

- A rate-limited read in `gh_api_with_retry` is detected from its stderr (`rate limit`, case-insensitive). It is not retried on the 2 s / 4 s schedule, and it returns a distinct exit code (`75`) that every existing `if !` / `||` caller still treats as a failure.
- `fetch_pr_state` reports `rate_limited` for that exit code and `unknown` for every other failure, exactly as before.
- In the Phase 4b poll loop, a `rate_limited` PR state is not counted toward `PR_STATE_FAILURE_LIMIT`. The loop waits for the limit to reset, using `GET /rate_limit` (`core.remaining == 0` → `core.reset - now + 1`; otherwise 60 s, GitHub's minimum wait for a secondary limit; unreadable → 60 s). The wait is capped at `DEADLINE`, and at `RETRY_REGISTRATION_DEADLINE` while no run is registered yet.
- The PR-closure check (`pr_closed_during_retry`) and the 4-failure breaker for real `unknown` reads are unchanged. When the deadline expires while rate-limited, the step fails closed with the existing `retry_timeout` status.
- A regression test runs the real Phase 4b retry block from the workflow with a stub `gh`. It feeds five consecutive rate-limited PR-state reads and then a successful one, and shows the step keeps polling the adopted run to completion and reaches attempt 2. Companion cases cover the rate-limited deadline expiry (fail closed, `retry_timeout`), a closed PR (still `pr_closed_during_retry`), and four plain failures (still `pr_state_check_failed`).

## Non-goals

- No change to `EDITOR_RETRY_BUDGET_MINUTES`, the 90-second registration window, the aggregator's status list, Phase 4 / Phase 6 / Phase 7 polling, or `scripts/comprehensive_test_and_release_gh_api.sh`.
- No reduction in the release gate's GitHub API usage. Whatever spent the token's budget on 2026-10-01 is outside this issue.
- No claim that the editor would have restored the canary in run 36797692597. The log ends before the adopted run's result.

## Constraints

- §6: no existing identifier, status value, env var, or log prefix is renamed or removed. New identifiers (`GH_API_RATE_LIMITED_RC`, `phase4b_rate_limit_wait_seconds`, `PR_STATE_RATE_LIMIT_WAITS`, `RATE_LIMIT_WAIT_DEADLINE`, `RATE_LIMIT_WAIT`) were checked for clashes in the workflow, `scripts/`, and `tests/`.
- §5: the change stays inside the Phase 4b step body and its test file, plus a changelog fragment.
- §9: YAML keeps 2-space indentation; Python tests use tabs, like the existing test file.
- §15: `GET /rate_limit` does not count against the REST rate limit, and it is read only after a rate-limited PR-state read, never on the normal path. No new per-iteration call is added on the normal path.
- §20: one fragment, `changelog.d/5858-phase4b-rate-limit-aware-pr-state.md` (`fixed`).
- §27: the workflow is 353,271 bytes before the change. The change adds well under 10 KB, which stays far below the 480,000-byte guard.

## Approach

Classify the failure where it happens, so the loop can act on it. `gh_api_with_retry` keeps each attempt's stderr separately. On a `rate limit` match it emits one warning and returns `75` without the short retries, which cannot succeed before the reset. `fetch_pr_state` turns `75` into `rate_limited`. The loop's `rate_limited` branch asks `phase4b_rate_limit_wait_seconds` how long to wait, logs a notice, sleeps, and falls through to the run read in the same iteration. When the PR read is limited but the run read is not (as in run 36797692597), a run that completes still ends the wait on time.

Alternatives considered:
- Waiting for the reset inside `gh_api_with_retry` for every caller. Rejected: the pre-loop reads have no deadline to cap the wait, and attempt 1 has nothing to wait for.
- Parsing `x-ratelimit-reset` from `gh api -i` on every call. Rejected: it changes the output format every caller parses. `GET /rate_limit` is free and already used by `workflow_retro.py`.

## Phases & Merge Strategy

This is a single-phase plan. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan for one standalone issue, and the change is one step body plus its test, which cannot be split usefully.

1. **Phase 1 — rate-limit-aware Phase 4b PR-state polling.**
   - Files: `.github/workflows/test-and-mark-stable.yml`, `tests/test_test_and_mark_stable_review_blocked_budget.py`, `changelog.d/5858-phase4b-rate-limit-aware-pr-state.md` [new].
   - Done when: the Goals above hold in the code; the new behavioural tests and every existing test in `tests/test_test_and_mark_stable_review_blocked_budget.py` pass under both `python3 <file>` (the CI form, `ci.yml:1050`) and `pytest`; the workflow parses as YAML and its Phase 4b body passes `bash -n`; and the size guard test passes.
   - Rollback: revert the phase PR. The gate goes back to counting rate-limited reads as `unknown`.

## Implementation Steps

1. In the Phase 4b step, define `GH_API_RATE_LIMITED_RC=75` before `gh_api_with_retry`. Make the helper capture each attempt's stderr into its own temporary file and append it to the combined log. On a case-insensitive `rate limit` match, warn once (`::warning::gh api rate-limited on attempt <n> …`) and return `GH_API_RATE_LIMITED_RC`. Update the helper's comment.
2. Make `fetch_pr_state` echo `rate_limited` when the helper returns `GH_API_RATE_LIMITED_RC`, and `unknown` on every other failure. Update its comment.
3. Add `phase4b_rate_limit_wait_seconds <deadline>`, defined next to `fetch_pr_state`: it reads `gh api rate_limit` for `core.remaining` and `core.reset`, computes the wait described in Goals, caps it at `<deadline> - now` (never below 0), and prints it.
4. In the poll loop, add a `rate_limited` branch before the `unknown` branch. It leaves `PR_STATE_FAILURES` unchanged, increments `PR_STATE_RATE_LIMIT_WAITS`, caps the wait at `RETRY_REGISTRATION_DEADLINE` while `RETRY_RUN_ID` is empty, logs a `::notice::`, and sleeps. Update the status comment block (`pr_state_check_failed` and `retry_timeout`) and the circuit-breaker comment.
5. Add behavioural tests to `tests/test_test_and_mark_stable_review_blocked_budget.py`. They slice the step's helper definitions and the retry block out of the workflow, run them in `bash` with a stub `gh` on `PATH` and a fake clock (`date` / `sleep` shell functions), and assert on `$GITHUB_OUTPUT` and the transcript. Also add contract assertions for the new branch.
6. Add the changelog fragment.

## Files & Modules

- `.github/workflows/test-and-mark-stable.yml` (Phase 4b step body only)
- `tests/test_test_and_mark_stable_review_blocked_budget.py`
- `changelog.d/5858-phase4b-rate-limit-aware-pr-state.md` [new]

## Tests

- New, behavioural (run the real step text):
  - five rate-limited PR-state reads, then `open`, while the adopted run goes `in_progress` → `completed/success`: no status is written, and the block reaches attempt 2;
  - PR-state reads rate-limited until the deadline: `status=retry_timeout`, never `pr_state_check_failed`, and the step's total waits stay within the deadline;
  - a closed PR during the wait: still `status=pr_closed_during_retry`;
  - four plain (not rate-limited) PR-state failures: still `status=pr_state_check_failed`.
- New contract assertions: the rate-limit branch precedes the `unknown` branch, the breaker counter is untouched there, and the wait is capped by `DEADLINE`.
- Existing: every test in the file, `tests/test_workflow_file_size_limit.py`, and a YAML parse of the workflow.
- End-to-end: the next `Test & Mark Stable Release` run. It is ACCEPTED that a real rate limit cannot be forced in CI. The behavioural test runs the exact step text instead.

## Risks & Mitigations

- A non-rate-limit error whose message contains "rate limit" would be waited on instead of counted. Mitigation: the wait is still bounded by the deadline, and the same match is already used by `gh_api_safe`.
- A long primary-limit reset (up to an hour) exceeds the remaining budget. Mitigation: the wait is capped at the deadline, and the step then fails closed with `retry_timeout`, as the issue requires.
- `GET /rate_limit` itself fails. Mitigation: a 60-second wait, still capped.
- While the PR read is rate-limited, a PR closure can go unseen until the next successful read. ACCEPTED: it is unchanged from today's `unknown` handling, and the run read still ends the wait.

## Rollout

No flag. The fix takes effect on the next run of `test-and-mark-stable.yml` from `main`. The workflow is internal to this repo and does not reach consumer repos through `workflow-templates/`. Rollback is a revert of the phase PR.

## References

- Issue #5858; failed run https://github.com/shubhodeep1/coding-workflows/actions/runs/36797692597; heal intake run https://github.com/shubhodeep1/coding-workflows/actions/runs/36803934783
- `scripts/comprehensive_test_and_release_gh_api.sh` (`gh_api_safe`), `scripts/workflow_retro.py` (`_gh_rate_limit_wait_seconds`)
- GitHub REST docs: rate limits and `GET /rate_limit` (does not count against the limit)

## Notes

- `security_pass_skip.py` verified the skip: `ai:workflow-heal: created and labelled by the issue automation`.

## Auto-decisions

- AD-1 [plan, 2026-10-01] What should `gh_api_with_retry` do on a rate-limited attempt? — Picked: A — stop retrying and return a distinct exit code 75 that existing callers still treat as failure. Alternatives: B — keep the 3 short retries and signal only on the last; C — wait for the reset inside the helper for every caller. Why: short retries cannot succeed before the reset, and only the poll loop has a deadline to cap a wait. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Where does the wait length come from? — Picked: A — `GET /rate_limit` (`core.remaining == 0` → reset + 1 s, otherwise 60 s, unreadable → 60 s), capped at the deadline. Alternatives: B — parse `x-ratelimit-reset` via `gh api -i` on every call; C — fixed exponential backoff. Why: the endpoint is free, already used by `scripts/workflow_retro.py`, and leaves every caller's output format unchanged. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-10-01] How does a rate-limited PR-state read affect the 4-failure breaker? — Picked: A — it neither increments nor resets `PR_STATE_FAILURES`. Alternatives: B — reset the counter; C — count it as before. Why: a rate limit says nothing about whether the PR is resolvable, and leaving the counter unchanged keeps the breaker as strict as today for real failures. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-10-01] Which status does a rate-limited deadline expiry report? — Picked: A — the existing `retry_timeout`. Alternatives: B — a new status value. Why: the aggregator and its status list stay unchanged (§5, §6), and the step log names the rate-limit waits. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-10-01] What form does the regression test take? — Picked: A — a behavioural test that runs the step's own text in bash with a stub `gh` and a fake clock, plus string contract checks. Alternatives: B — string contract checks only. Why: the issue asks to confirm the gate keeps verifying the adopted run, which only running the code shows. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-10-01] Should a rate-limit wait extend the 90-second registration window on the dispatch path? — Picked: A — no; cap the wait at the registration deadline while no run is registered. Alternatives: B — extend the window by the wait. Why: the issue is about the adopted-run path, and capping keeps the dispatch path's timing exactly as today. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-10-01] Which docs change? — Picked: A — the step's own comments plus a changelog fragment; README.md and agents.md do not describe Phase 4b polling. Alternatives: B — also add an agents.md note. Why: §5 minimal change; the behaviour is documented where operators read it (the step log and comments). Applied in: phase 1 PR. Status: pending review
