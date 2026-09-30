# smoke_review_checked_out_sha: an empty job-log body is retryable, not "no line"

Source issue: shubhodeep1/coding-workflows#5689 (https://github.com/shubhodeep1/coding-workflows/issues/5689)
Base branch: claude/implement-plan-issue-4898-retrigger-dispatch-default-branch
Security pass: run

## Summary

`smoke_review_checked_out_sha` in `scripts/smoke_review_dispatch.sh` returns 2 ("the log was read but has no such line") when the `codex-agent` job-log read succeeds with an empty body. GitHub serves an empty body for a job whose log is still being stored, so the E2E smoke gate in `.github/workflows/test-and-mark-stable.yml` can reject a genuine review run on a timing race. Return 1 (retryable) for an empty or whitespace-only body instead.

## Context

- PR #5107 (issue #5093) added `scripts/smoke_review_dispatch.sh` on the base branch; it is not on `main` yet.
- The function reads the job log into a temp file and greps for the first line-anchored `Captured INITIAL_HEAD_SHA=<sha> for stale-base detection.` line (`scripts/smoke_review_dispatch.sh`, `smoke_review_checked_out_sha`). An empty file leaves `line` empty, so it returns 2.
- Both callers already treat rc=1 as "retry" with a bounded budget, and rc=2 as a definite miss:
  - Phase 4 leg (c) (`test-and-mark-stable.yml`, the `BUG_B_SHA_RC` branch): rc=1 logs "checked-out commit not readable yet; retrying next poll"; rc=2 sets `BUG_B_STATE="rejected"` for the rest of the step.
  - Phase 4b (`RETRY_CHECKED_OUT_RC`): rc=1 sets `RETRY_TRANSIENT` and retries every 15 s until `DEADLINE`; rc=2 ends in `retry_run_unverified`.
- The blocker on #5520 found the defect while comparing #5520's closed PR #5561 (which fixed the same bug in its own helper) with #5093's helper.

## Goals

- A log read that succeeds with an empty body, or a body with no non-whitespace byte (spaces, tabs, `\r`, `\n` only), returns 1 and prints nothing.
- rc=2 stays reserved for a non-empty log with no matching line.
- The first-match rule is unchanged: the first line-anchored checkout line wins, and a later line printed from reviewed content never does.
- The function still issues at most one jobs read and one log read per call (§15).

## Non-goals

- No change to `test-and-mark-stable.yml`: both callers already retry rc=1 within their own deadline.
- No in-helper retry loop, and no change to `smoke_review_pr_named_runs` or `smoke_review_sha_descends_from`.
- The same defect in any other helper (#5561's helper was closed as superseded).

## Constraints

- §5: the smallest change that fixes the defect; no reformatting.
- §6: no identifier, return code, output, or status is renamed or repurposed. rc=1 already means "retryable"; this adds one more case to it.
- §9: tabs in the shell script and the Python test file (both already tab-indented).
- §15: no new GitHub API call. The emptiness check reads the temp file the function already wrote.
- §20: a `changelog.d/` fragment, because the smoke gate's failure mode changes.

## Approach

After the log read succeeds and before the grep, check the temp file for any non-whitespace byte with `grep -aq '[^[:space:]]' "${log_file}"`. If there is none, remove the file and return 1. Reading the file directly (no pipe) keeps the check correct under a caller's `set -o pipefail`: a pipe into `grep -q` could let `tr` die of SIGPIPE and turn a non-empty log into a false "empty". `\r` is in `[:space:]`, so a CRLF-only body counts as empty too. Update the function's `Returns:` comment to name the new rc=1 case.

Alternative considered: retry the log read a bounded number of times inside the helper (see AD-1). Rejected because it adds API reads and sleeps to a function whose documented budget is one log read, while both callers already retry rc=1 on their own schedule.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: the fix is one function and its tests, and it cannot usefully be split.

1. **Phase 1 — empty job-log body is retryable.**
   - Files: `scripts/smoke_review_dispatch.sh`, `tests/test_smoke_review_dispatch.py`, `changelog.d/5689-smoke-empty-job-log-retryable.md` [new].
   - Done: the helper returns 1 for an empty and a whitespace-only body, 2 for a non-empty body with no line, 0 with the SHA for a genuine line; `tests/test_smoke_review_dispatch.py` passes in full.
   - Rollback: revert the phase PR; the helper returns to the old rc=2 behaviour.

## Implementation Steps

1. `scripts/smoke_review_dispatch.sh`, `smoke_review_checked_out_sha`: after the successful `gh api --allow-escape-sequences … > "${log_file}"`, add the whitespace-only check that removes the temp file and returns 1. Update the `Returns:` line of the doc comment: `1 API failure, malformed jobs payload, or a log body that is empty or whitespace-only (the job's log may still be being stored) (retryable)`.
2. `tests/test_smoke_review_dispatch.py`: add a test that covers an empty body, a whitespace-only body (spaces, tabs, `\n`, `\r\n`), a non-empty body with no line (still 2), and a genuine line (0 with the SHA), each asserting exactly one jobs read and one log read.
3. `changelog.d/5689-smoke-empty-job-log-retryable.md` [new], section `fixed`, per §20.D.

## Files & Modules

- `scripts/smoke_review_dispatch.sh`
- `tests/test_smoke_review_dispatch.py`
- `changelog.d/5689-smoke-empty-job-log-retryable.md` [new]

## Tests

- Unit (stub `gh`): the new test above, plus the existing `test_checked_out_sha_*` tests unchanged.
- Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/test_smoke_review_dispatch.py -q` and the ci.yml step form `PYTHONDONTWRITEBYTECODE=1 python3 tests/test_smoke_review_dispatch.py`.
- `bash -n scripts/smoke_review_dispatch.sh` and `shellcheck` if installed.

## Risks & Mitigations

- A job whose log stays empty forever is now retried instead of rejected. Mitigation: both callers bound the retry (Phase 4 by its poll deadline, Phase 4b by `DEADLINE`, ending in `retry_run_unverified`), so the gate still ends; it cannot pass without a matching checkout line.
- A forged checkout line cannot exploit the change: an empty body contains no line at all, and the first-match rule for non-empty bodies is unchanged.

## Rollout

Ships with the base branch's project (`claude/implement-plan-issue-4898-retrigger-dispatch-default-branch` → its final PR). `scripts/` reaches consumer repos with the next `@stable` sync; no variable, flag, or migration.

## Auto-decisions

- AD-1 [plan, 2026-09-30] How should an empty job-log body be handled? — Picked: A — return 1 so the caller retries on its own schedule. Alternatives: B — retry the log read a bounded number of times inside the helper; C — both. Why: both callers already retry rc=1 within a deadline, and A keeps the helper's one-log-read budget (§15, §5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What counts as whitespace-only? — Picked: A — no byte outside `[:space:]` (so `\r`, `\n`, spaces, and tabs only). Alternatives: B — only a zero-byte body; C — also treat an ANSI-escape-only body as empty. Why: the issue names whitespace-only bodies; an escape-only body is not whitespace and is not a known GitHub response, so it stays rc=2 (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Update `agents.md` or the workflow comments? — Picked: A — no; update only the helper's doc comment and add a changelog fragment. Alternatives: B — also add the rc semantics to the `agents.md` smoke-job entry. Why: `agents.md` and the workflow comments do not document the helper's return codes, and Phase 4's comment already says an unreadable log is retried (§5). Applied in: phase 1 PR. Status: pending review

## Notes

- The security-pass skip check (`.claude/scripts/security_pass_skip.py`) reported `no skip label`, so the security pass runs.

## References

- Issue #5689; related #5520, #5093, #5094.
- PR #5107 (added the helper), PR #5561 (closed; fixed the same defect in its own helper).
- `docs/completed/issue-5093-smoke-review-dispatch-default-branch-plan.md`.
