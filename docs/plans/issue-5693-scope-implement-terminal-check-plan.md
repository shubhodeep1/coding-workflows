# Scope the release smoke test's "Implement finished" decision to the smoke issue's own runs

Source issue: shubhodeep1/coding-workflows#5693 (https://github.com/shubhodeep1/coding-workflows/issues/5693)
Base branch: stable
Security pass: skip (ai:workflow-heal: automation-produced issue)

## Summary

`Test & Mark Stable Release` run 36717635823 failed at `Phase 3b: Wait for PR creation (implement phase)` with `All 1 implement workflow run(s) completed but no PR was created`, while the smoke issue's own Implement run was still in progress. This plan makes the step confirm, before it declares Implement finished, that the smoke issue's own non-skipped Implement runs have completed. Page 1 of the unscoped run list stays the progress signal only.

## Context

- The `wait-implement` step in `.github/workflows/test-and-mark-stable.yml` (stable, lines 1108–1312) polls `repos/<repo>/actions/runs?per_page=50&created=>…` every `POLL_INTERVAL` and counts Implement-named, non-skipped runs on that one page **without a title filter** (`IMPL_RUN_TOTAL`, `IMPL_RUN_IN_PROGRESS`, lines 1248–1256). When that count shows runs and none active after the 60-second grace period, it rechecks for the PR once and fails with `status=implement_failed` (lines 1287–1297).
- Evidence from the failed run's own log and the run list (not guesswork):
  - The smoke issue #5673 (`[E2E Smoke Test] Update smoke-test canary (run 36717635823)`) was approved at about 13:14:10Z. Its Implement run `36720184070` was created at 13:14:14Z and **completed at 13:19:55Z with `success`**.
  - At 13:17:14Z the page-1 poll saw `1 total, 0 active`. 80 runs were created in this repository between 13:13 and 13:18, most of them skipped `issue_comment` runs, so run `36720184070` had slid past the newest 50. The one completed Implement run left on the page was the parallel alt-model job's cancelled run `36720276447`.
  - The step failed at 13:17:24Z with `All 1 implement workflow run(s) completed but no PR was created`, and cleanup closed #5673 at the same second. The real run finished 2.5 minutes later with no issue left to open a PR for.
- Issue #4723 (merged into `stable` as #4729) already paged and title-scoped the **run-ID capture** in this step (`capture_run_id`, `require_scoped_run_id`, `SCOPED_RUN_LOOKUP_MAX_PAGES=10`). The terminal "all runs completed" decision was left on the unscoped single page.
- Regression coverage for this step lives in `tests/test_test_and_mark_stable_plan_polling_guard.py`, run by the `Test-and-mark-stable plan polling guard regression tests` step of `.github/workflows/ci.yml` (stable, line 914).

## Goals

- `wait-implement` never fails with `status=implement_failed` while a non-skipped Implement run whose `display_title` is the smoke issue's title is queued or in progress, wherever that run sits in the `created=>` window (up to `SCOPED_RUN_LOOKUP_MAX_PAGES` pages of 100).
- Before failing, the step walks the window for the issue's own Implement runs. It fails only when that walk read to the end of the window (a short page) and found no active issue-scoped run.
- An unreadable page, or a walk that reads every page up to the cap without reaching the end, is treated as **unknown**: the step keeps waiting, still bounded by `PHASE_TIMEOUT` inactivity and the job's `timeout-minutes`.
- The failure line names the issue-scoped result: the newest issue-scoped run's ID and conclusion, or that no issue-scoped run ran at all.
- While the issue's run is active, later cycles re-read only that run by ID (one call), and walk again only once it has completed or cannot be read.
- The PR recheck, the 60-second grace period, the inactivity limit, `status=implement_failed`, and the `::error::All <n> implement workflow run(s) completed but no PR was created` prefix all stay.
- A behavioural test reproduces the incident: unrelated completed Implement runs fill page 1 while the issue's run is active on page 2. It must keep waiting and then succeed when the PR appears.

## Non-goals

- The page-1 poll's progress metrics (`IMPL_RUN_TOTAL`, `IMPL_RUN_IN_PROGRESS`, `IMPL_RUN_STATUS`, the step-name and branch-head signals) and the activity detection built on them are unchanged (AD-1).
- Detecting an issue-scoped Implement failure while unrelated Implement runs keep page 1 active. That case waits for the inactivity limit today and still does (see Risks).
- `wait-clarify`, `wait-plan`, deep verification, and the final gate are unchanged.

## Constraints

- §5 minimal change set: the `wait-implement` step body, its regression test, and a changelog fragment.
- §6 naming immutability: existing functions (`capture_run_id`, `require_scoped_run_id`), variables, outputs (`run_id`, `pr_number`, `status`), status values (`implement_failed`, `timeout`, `run_id_missing`), and the `::error::All … implement workflow run(s) completed but no PR was created` prefix are kept (AD-4). New identifiers (`summarize_scoped_impl_runs`, `IMPL_SCOPED_ACTIVE_RUN_ID`, `IMPL_SCOPED_RC`, `IMPL_SCOPED_SUMMARY`, `IMPL_SCOPED_TOTAL`, `IMPL_SCOPED_ACTIVE`, `IMPL_SCOPED_NEWEST_ID`, `IMPL_SCOPED_NEWEST_CONCLUSION`, `IMPL_SCOPED_ACTIVE_ID`, `IMPL_SCOPED_CACHED_STATUS`) were checked for collisions in the workflow file: none exist.
- §15 API hygiene: the walk runs only at the terminal decision (page 1 shows Implement runs, none active, grace over, no PR), not on every poll. It stops at the first page holding an active issue-scoped run or at a short page, and it is capped at 10 pages. While the issue's run stays active, each later decision costs one by-ID read instead of a walk (AD-3).
- §9: YAML stays 2-space; the Python test uses tabs.
- §20: changelog fragment, since the release gate's failure mode changes.
- §27: the workflow is 364,837 bytes on `stable`, far below the 480,000-byte guard.
- Issue fix constraints: nothing is disabled, skipped, or weakened. A genuine issue-scoped Implement failure, or an Implement run that never ran for the issue, still fails with `implement_failed` (AD-2).

## Approach

Keep the unscoped page-1 poll as the trigger and progress signal (AD-1). Add one helper, `summarize_scoped_impl_runs`. It walks `actions/runs?per_page=100&page=<n>&created=>${IMPL_CREATED_AFTER}` with the same shape guard and page cap as `capture_run_id`, and filters to Implement-named, non-skipped runs whose `display_title` is `ISSUE_TITLE`. It stops at the first page holding an active run or at a short page. It prints `<total> <active> <newest id> <newest conclusion> <newest active id>` and returns 0, or returns 1 on an unreadable page and 2 when it reached the cap with every page full.

In the terminal branch, after the existing PR recheck:

1. If a cached active run ID exists, read that run by ID. If it is still not `completed`, keep waiting without a walk (AD-3).
2. Otherwise walk. If the walk completed (rc 0) with no active issue-scoped run, fail with `status=implement_failed`, keeping the `All <n> …` prefix and appending the issue-scoped run ID and conclusion, or saying that no issue-scoped run ran (AD-2, AD-4).
3. On an active run, cache its ID and keep waiting. On rc 1 or 2, keep waiting as unknown. "Keep waiting" falls through to the loop's existing inactivity check and poll sleep, so `PHASE_TIMEOUT` still bounds it (AD-5).

Alternatives considered: walking every poll cycle (up to 10 calls per 10-second poll, §15), and title-filtering only the page-1 poll (it loses the run as soon as it leaves page 1, which is exactly this incident). Both are recorded under AD-1.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan, and the change is one step body plus its test.

1. **Phase 1 — confirm the issue's own Implement runs finished before failing `wait-implement`.**
   - Files: `.github/workflows/test-and-mark-stable.yml` (`wait-implement` step), `tests/test_test_and_mark_stable_plan_polling_guard.py`, `changelog.d/5693-scope-implement-terminal-check.md` [new].
   - Done when: the new behavioural tests pass (incident shape waits then succeeds; genuine issue-scoped failure fails with its run ID and conclusion; no issue-scoped run fails; unreadable and capped walks wait until the inactivity limit; a cached active run is re-read by ID and re-walked once it completes), every existing test in the file still passes, the workflow parses as YAML, and its size stays under 480,000 bytes.
   - Rollback: revert the phase PR. The step returns to the unscoped single-page terminal decision.

## Implementation Steps

1. `.github/workflows/test-and-mark-stable.yml`, `wait-implement` step, after `require_scoped_run_id`: add `summarize_scoped_impl_runs` as described in Approach, with a comment citing run 36717635823 and issue #5693. Initialise `IMPL_SCOPED_ACTIVE_RUN_ID=""` before the poll loop.
2. Same step, the `IMPL_RUN_TOTAL > 0 && IMPL_RUN_IN_PROGRESS == 0 && grace over` branch (stable lines 1287–1297): after the PR recheck, add the cached-run read and the walk, fail only on a completed walk with no active issue-scoped run, and otherwise echo a waiting line (the active run's ID, or why the result is unknown) and fall through to the inactivity check.
3. `tests/test_test_and_mark_stable_plan_polling_guard.py`: extend the stub `gh` additively (a PR that appears after N pull reads, run-by-ID reads, jobs and commits reads, and a switch to later run pages after N pull reads), add an optional stub `date` clock so the 60-second grace period elapses, and add the behavioural cases from the done condition.
4. `changelog.d/5693-scope-implement-terminal-check.md` with `<!-- changelog: fixed -->`.

## Files & Modules

- `.github/workflows/test-and-mark-stable.yml`
- `tests/test_test_and_mark_stable_plan_polling_guard.py`
- `changelog.d/5693-scope-implement-terminal-check.md` [new]

## Tests

- Behavioural: new cases in `tests/test_test_and_mark_stable_plan_polling_guard.py` run the real `wait-implement` script against the stubbed `gh` (already wired into `ci.yml`).
- Existing: every test in the same file stays green, and the stub changes are additive.
- End to end: the next `Test & Mark Stable Release` run exercises the step against the live repository.

## Risks & Mitigations

- An issue-scoped Implement run that ends without a PR while unrelated Implement runs keep page 1 active is not caught by the terminal branch. ACCEPTED: that is today's behaviour. The inactivity limit and the job's `timeout-minutes` still bound the wait (AD-1).
- The smoke issue's Implement run is not yet indexed 60+ seconds after `/approved`, while an unrelated Implement run has completed. The complete walk then finds no issue-scoped run and fails. ACCEPTED: the same false failure existed before, and the failure line now says that no issue-scoped run ran (AD-2).
- Extra API calls at the terminal decision. Mitigation: the walk is capped at 10 pages, stops early, and is replaced by one by-ID read while the run stays active (AD-3).
- An unknown result waits instead of failing. Mitigation: it falls through to the unchanged inactivity check (AD-5).

## Rollout

No flag. The fix lands on `stable` through the final PR, and `forward-merge-stable-to-main.yml` carries it to `main`. `test-and-mark-stable.yml` runs only in this repository, so no consumer propagation is needed (§14). Rollback is a revert of the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which runs decide that Implement has finished? — Picked: A — keep the unscoped page-1 poll as the progress signal and trigger, and confirm with a paged, issue-scoped walk before failing. Alternatives: B — walk every poll cycle; C — title-filter the page-1 poll only. Why: the issue's suggested fix, and §15 (the walk runs only at the terminal decision); C loses the run as soon as it leaves page 1, which is this incident. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] A complete walk finds no non-skipped Implement run for the smoke issue: what does the step do? — Picked: A — fail with `status=implement_failed` as before, saying no issue-scoped run ran. Alternatives: B — keep waiting. Why: the fix constraints forbid weakening the guard, and B could poll up to the job's 300-minute limit, because unrelated runs keep resetting the inactivity timer; A's only false-failure risk (the run not indexed 60+ seconds after `/approved`) existed before. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] While the smoke issue's Implement run is active, how is the next terminal decision checked? — Picked: A — remember that run's ID and re-read only that run, walking again once it has completed or cannot be read. Alternatives: B — walk again every cycle. Why: §15 cycle-local caches; one call instead of up to 10. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] What does the failure line say? — Picked: A — keep `::error::All <n> implement workflow run(s) completed but no PR was created` and `status=implement_failed`, with `<n>` the issue-scoped count, and append the issue number, the newest issue-scoped run ID, and its conclusion. Alternatives: B — new wording and a new status value. Why: §6 and the issue's fix constraints (never rename log prefixes); the appended detail makes a genuine Implement failure diagnosable. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] How does an unknown walk keep waiting? — Picked: A — fall through to the loop's existing inactivity check and poll sleep. Alternatives: B — `continue` with its own idle check, like `wait-plan`'s `fail_plan_confirm_retry_if_idle`. Why: §5; the same `PHASE_TIMEOUT` bound without a new helper. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Which docs change? — Picked: A — a changelog fragment only. Alternatives: B — also add an `agents.md` paragraph. Why: neither `README.md` nor `agents.md` documents the `wait-implement` terminal check, and #4723 made the same call for this step (§5). Applied in: phase 1 PR. Status: pending review

## Notes

- `.claude/scripts/security_pass_skip.py` reported `{"skip": true, "label": "ai:workflow-heal", "reason": "ai:workflow-heal: created and labelled by the issue automation"}`.

## References

- Issue #5693 (this heal), smoke issue #5673, failed run https://github.com/shubhodeep1/coding-workflows/actions/runs/36717635823, heal intake run https://github.com/shubhodeep1/coding-workflows/actions/runs/36722649881.
- Issue #4723 and its plan `docs/completed/issue-4723-require-plan-run-id-plan.md` (paged, title-scoped run-ID captures).
