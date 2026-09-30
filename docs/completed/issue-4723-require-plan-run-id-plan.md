# Require a captured Plan run ID before the release smoke test reports Plan success

Source issue: shubhodeep1/coding-workflows#4723 (https://github.com/shubhodeep1/coding-workflows/issues/4723)
Base branch: stable
Security pass: skip (ai:workflow-heal: automation-produced issue)

## Summary

`Test & Mark Stable Release` run 36374918973 blocked at deep verification because the `Phase 2: Wait for plan to complete` step reported `status=success` with an empty Plan run ID. This plan makes that step find the Plan run even when it has fallen off the first 100-run page, and fail at Plan capture, not later at deep verification, when no issue-scoped Plan run ID can be found.

## Context

- The `wait-plan` step in `.github/workflows/test-and-mark-stable.yml` (stable, lines 722–901) looks up the Plan run with `latest_scoped_run_field`, which reads **one** page of `repos/<repo>/actions/runs?per_page=100&created=>…` and filters by workflow name (`Plan`), `display_title == ISSUE_TITLE`, and `conclusion != "skipped"` (lines 748–759).
- On `ai:awaiting-approval` or `ai:implementing` it calls `capture_run_id "Plan"` and writes `run_id=<result>` and `status=success` / `status=auto_approved` even when the result is empty (lines 827–841).
- The failed run's own log (evidence, not guesswork): the step polled `plan_run: in_progress` until 03:56:48Z, saw `ai:awaiting-approval` at 03:57:00Z, and wrote an empty `run_id`. No `::error::gh api call failed` line appears, so the API read succeeded and the filter matched nothing. Re-reading the window afterwards, the Plan run `36375309681` (created 03:50:56Z) sits at index 96 of the first 100-run page at 03:56:48Z and index 98 at 03:57:10Z. 166 runs were created in this repository between issue creation (03:46:08Z) and 03:57:12Z, most of them skipped `issue_comment` runs. The run had reached the end of the single page the lookup reads. With a few more runs, or runs deleted since, it drops off the page and the lookup returns empty.
- `Phase 2b: Soft-error analyser (plan)` is skipped when `run_id` is empty (line 905). `Deep verify` reports `Plan: run ID not found` (lines 3139–3144), and the final gate blocks on `deep_passed` (lines 3868–3875). The result: a release that is blocked for a capture defect, with zero failed steps in the counts.
- Existing regression coverage for this step lives in `tests/test_test_and_mark_stable_plan_polling_guard.py`, run by the `Test-and-mark-stable plan polling guard regression tests` step of `.github/workflows/ci.yml`.

## Goals

- `latest_scoped_run_field` finds the newest issue-scoped, non-skipped Plan run when it is on a later page of the `created=>` window. It walks pages, stops at the first page holding a match or at a short page, and is bounded to 10 pages. The 10-second status poll keeps its one-page read (AD-5).
- The lookup still accepts only runs whose name matches the regex, whose `display_title` equals the issue title, and whose conclusion is not `skipped`. No unrelated run is ever accepted.
- Both success exits of `wait-plan` (`status=success`, `status=auto_approved`) require a numeric Plan run ID. Without one the step emits `::error::…`, writes `status=run_id_missing`, and exits 1, so the gate reports `Plan ....... FAILED (run_id_missing)` at the Plan line.
- A behavioural regression test runs the real `wait-plan` script against a stubbed `gh`. It covers the three cases: label present and no matching run (must fail at capture), matching run only on page 2 (must succeed with that ID), and a same-name run with a different title on page 1 (must not be accepted).

## Non-goals

- ~~Clarify (`wait-clarify`) and Implement (`wait-implement`) run-ID capture. They share the single-page pattern but did not fail here (AD-1).~~ Changed by AD-8 (conformance run 2): the 2026-09-29 occurrence on #4723 (run 36504041362) failed at Implement capture, so both sibling captures are now paged, title-scoped, and required, like Plan.
- ~~The `OTHER_ACTIVE_PLAN_RUNS` check in the `completed` branch keeps reading page 1 via `fetch_plan_runs_json`.~~ Changed by AD-6 (PR #4730 review round 2): when page 1 holds no active run, the check walks later pages of the same query under the same shape guard and page cap. Its page-1 read, shape-validation guard, and existing tests are unchanged.
- Deep verification and the final gate logic are unchanged. A missing ID must still fail the release.

## Constraints

- §5 minimal change set. Only the `wait-plan` step body, its regression test, and a changelog fragment.
- §6 naming immutability. Existing functions (`fetch_plan_runs_json`, `latest_scoped_run_field`, `capture_run_id`), outputs (`run_id`, `status`), and status values are kept. New identifiers (`fetch_plan_runs_page_json`, `PLAN_RUN_LOOKUP_MAX_PAGES`, `require_plan_run_id`, `fail_plan_confirm_retry_if_idle` (AD-7), the status value `run_id_missing`) were checked for collisions in the workflow file.
- §15 API hygiene. The common case stays at one call per poll (a match on page 1). Extra pages are read only while page 1 is full and holds no match, capped at 10 pages, which is also the ceiling of GitHub's 1,000-result list window.
- §9 YAML stays 2-space. The Python test uses tabs.
- §20 changelog fragment, since the release gate's failure mode changes.
- §27 workflow size. The file is 350,650 bytes, far below the 480,000-byte guard.
- Issue fix constraints: nothing is disabled, skipped, or weakened. The missing-ID case still fails, only earlier and with a precise status.

## Approach

Page walking inside the existing scoped lookup (AD-2), plus a guard on both success exits (AD-3). Alternatives considered: querying the Plan workflow's own run list (`actions/workflows/<file>/runs`) needs the workflow file name of `TEST_REPO`, which differs between this repository and consumer test repositories, and still overflows, because every `issue_comment` creates a skipped Plan run. Keeping one page and only failing earlier would turn this false release block into a different false release block.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan, and the change is one step body plus its test.

1. **Phase 1 — paginate the scoped Plan run lookup and require the ID before success.**
   - Files: `.github/workflows/test-and-mark-stable.yml` (`wait-plan` step), `tests/test_test_and_mark_stable_plan_polling_guard.py`, `changelog.d/4723-require-plan-run-id.md` [new].
   - Done when: the new behavioural tests pass, the existing guard tests still pass, `actionlint`/YAML parse of the workflow is clean, and the gate's Plan line reads `FAILED (run_id_missing)` for a missing ID (asserted by the test via the step's `GITHUB_OUTPUT`).
   - Rollback: revert the phase PR. The step returns to single-page lookup and success-without-ID.

## Implementation Steps

1. `.github/workflows/test-and-mark-stable.yml`, `wait-plan` step: add `PLAN_RUN_LOOKUP_MAX_PAGES=10` and `fetch_plan_runs_page_json <page>` (same query as `fetch_plan_runs_json` plus `&page=<n>`). Rewrite the body of `latest_scoped_run_field` to walk pages 1..cap with the unchanged jq filter, return the first page's match, stop on a short page, and return 1 on a failed read.
2. Same step: add `require_plan_run_id <id> <label>`, which returns when the ID is numeric, and otherwise emits `::error::`, writes `status=run_id_missing`, and exits 1. Call it after `capture_run_id "Plan"` in both the `ai:awaiting-approval` and `ai:implementing` branches, before the outputs are written.
3. `tests/test_test_and_mark_stable_plan_polling_guard.py`: add a harness that extracts the `wait-plan` `run:` body from the YAML, substitutes the `${{ steps.create-issue.outputs.created_after }}` expression, puts stub `gh` and `sleep` executables on `PATH`, runs it with `bash`, and reads `GITHUB_OUTPUT`. Add the three behavioural cases from Goals.
4. `changelog.d/4723-require-plan-run-id.md` with `<!-- changelog: fixed -->`.

## Files & Modules

- `.github/workflows/test-and-mark-stable.yml`
- `tests/test_test_and_mark_stable_plan_polling_guard.py`
- `changelog.d/4723-require-plan-run-id.md` [new]

## Tests

- Unit/behavioural: the three new cases in `tests/test_test_and_mark_stable_plan_polling_guard.py`, which run the real step script with a stubbed `gh`. They are already wired into `ci.yml`.
- Existing: the three static guard tests in the same file stay green.
- End to end: the next `Test & Mark Stable Release` run exercises the step against the live repository.

## Risks & Mitigations

- A busy window where the Plan run is more than 1,000 runs back. Mitigation: the step now fails at capture with `run_id_missing`, a precise signal, rather than a misleading deep-verify failure. ACCEPTED, since GitHub's list window caps at 1,000 results.
- Extra API calls while the Plan run is on page 2+. Mitigation: bounded at 10 reads per lookup, and only when page 1 is full with no match.
- A failed read on page 2+ makes the lookup return 1. Callers already treat that as empty/`unknown` and retry (`capture_run_id` retries 5 times).

## Rollout

Lands on `stable` through the project's final PR (the heal issue's `Target branch:`). It reaches `main` through the existing stable→main forward merge. It is an internal workflow only (no `workflow-templates/` copy), so there is no consumer propagation.

## Auto-decisions

- AD-1 [plan, 2026-09-28] Scope: fix only `wait-plan`, or also Clarify/Implement run-ID capture? — Picked: A — Plan only, as the issue specifies. Alternatives: B — also harden `wait-clarify` and `wait-implement`. Why: §5 minimal change. The evidence implicates only Plan, whose capture happens after the longest burst of runs. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How to narrow or paginate the lookup? — Picked: A — bounded page walk (≤10 pages) of the existing scoped query, stopping at the first page with a match or a short page. Alternatives: B — per-workflow run list (`actions/workflows/<file>/runs`); C — keep one page and only fail earlier. Why: B needs a repo-specific workflow file and still overflows on skipped `issue_comment` runs; C turns one false block into another. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] What does `wait-plan` report when no ID is found? — Picked: A — new status value `run_id_missing`, `::error::`, exit 1. Alternatives: B — reuse `status=plan_failed`. Why: the gate prints the status verbatim, so a distinct value names the cause, and every non-success value already fails the Plan line. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Make the page cap configurable? — Picked: A — a step-local constant `PLAN_RUN_LOOKUP_MAX_PAGES=10`, no new env var. Alternatives: B — a new env var with default 10. Why: §5. 10 pages is GitHub's 1,000-result ceiling for this list, so a larger value buys nothing. Applied in: phase 1 PR. Status: pending review
- AD-5 [phase 1/1, 2026-09-28] Should the 10-second status poll page through runs too? — Picked: A — no, the poll keeps reading one page (`latest_scoped_run_field "Plan" "status" 1`), and only the run-ID capture at the success exits pages. Alternatives: B — the poll pages like the capture (up to 10 calls per poll while no run matches, about 3,600 calls an hour); C — the poll reads 2 pages. Why: §15, since the poll is an activity signal and its per-poll cost stays one call as before. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1 — review round 2, 2026-09-28] Should the "other active Plan runs" check in `wait-plan` page past the first 100 runs, although the plan listed it as a Non-goal? — Picked: A — yes: when page 1 finds no active run, walk later pages of the same query with the same shape guard and `PLAN_RUN_LOOKUP_MAX_PAGES` cap, stopping at the first page with an active run or a short page. Alternatives: B — reject the review finding as out of scope. Why: an older, still-active real Plan run behind 100 newer runs is the #4723 false release block in the same step; the branch is rare (Plan completed without labels), so the extra reads stay bounded. Applied in: phase 1 PR (review round 2). Status: pending review
- AD-7 [phase 1/1 — review round 3, 2026-09-28] How should the "unable to confirm concurrent Plan runs" retries in `wait-plan` be bounded, since they `continue` before the loop's inactivity check? — Picked: A — apply the existing inactivity limit inside both retries (page 1 and later pages): once no activity was seen for `PLAN_PHASE_TIMEOUT` minutes, fail with `status=timeout`. Alternatives: B — a new consecutive-retry counter; C — add exponential backoff around the retry. Why: §5, it reuses the step's own stall rule, and `gh_api_safe` already backs off on rate limits; the page-1 retry had the same gap on the base branch. Applied in: phase 1 PR (review round 3). Status: pending review
- AD-8 [conformance 2/3, 2026-09-29] The 2026-09-29 occurrence on #4723 (run 36504041362) failed at `wait-implement`'s run-ID capture (one page, no issue-title filter), and `wait-clarify` has the same code. Should the conformance fix extend the #4723 fix to both sibling captures, although AD-1 scoped the project to Plan? — Picked: A — yes: both captures are issue-title scoped, walk at most 10 pages, and require a numeric ID before `status=success` (`run_id_missing`). Alternatives: B — `wait-implement` only; C — keep AD-1 and only record the finding. Why: the recorded occurrence falsifies AD-1's premise, and both siblings share the same failure mode and the same deep-verification symptom; without the title filter a paged lookup would take the parallel alt-model job's newer run. Applied in: conformance fix PR 2. Status: pending review
- AD-9 [conformance 2/3 — review round 1, 2026-09-29] Which status should `wait-clarify` and `wait-implement` write when `ISSUE_TITLE` is empty (the new up-front guard, mirroring `wait-plan`'s)? — Picked: A — the existing `run_id_missing`, with `::error::Missing issue title for <phase> run scoping`. Alternatives: B — a per-phase failure value (`clarify_failed`, new; `implement_failed`, which already means the Implement workflow failed). Why: §5 and §6, it adds no new status identifier and does not overload `implement_failed`; without a title the run ID cannot be captured, and the error line names the cause. Applied in: conformance fix PR 2 (#5035, review round 1). Status: pending review
- AD-10 [conformance 2/3 — review round 1, 2026-09-29] The reviewer panel asked to consolidate the run-ID capture copied across `wait-clarify`, `wait-plan`, and `wait-implement`. Refactor now? — Picked: A — no: keep the per-step copies and reject the finding as an out-of-scope refactor. Alternatives: B — move one paged, title-scoped lookup into the already-sourced `scripts/comprehensive_test_and_release_gh_api.sh` and call it from all three steps. Why: §5 and §12.C, the copies behave the same and are each covered by behavioural tests, while B rewrites `wait-plan`'s capture (return codes 1/2, the one-page poll cap, the other-active walk) that already passed three review rounds, in a release gate. Applied in: no code change. Status: pending review

## Notes

- `security_pass_skip.py` verdict: `{"skip": true, "label": "ai:workflow-heal", "reason": "ai:workflow-heal: created and labelled by the issue automation"}`.

## References

- Issue #4723; failed run https://github.com/shubhodeep1/coding-workflows/actions/runs/36374918973; heal intake run https://github.com/shubhodeep1/coding-workflows/actions/runs/36379107367.
