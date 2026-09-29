<!-- changelog: fixed -->
- **The release smoke test no longer reports Clarify, Plan, or Implement success without that phase's run ID.** `test-and-mark-stable.yml` now finds each phase's run past the first 100 runs, only for the smoke issue's own title, and when no run can be found the phase fails at capture with `status=run_id_missing`.

Run 36374918973 blocked the stable release at `Internals .. FAILED` even though its totals showed 0 failed steps. The Plan wait step read one 100-run page of `actions/runs`. As skipped `issue_comment` runs piled up, the smoke issue's Plan run slid to index 96 of that page and then off it, so the step wrote `status=success` with an empty `run_id`. That skipped `Phase 2b: Soft-error analyser (plan)` and left deep verification to fail the release as `Plan: run ID not found`. Run 36504041362 failed the same way at Implement: the smoke issue's Implement run sat at index 137 of the window when the PR appeared. The Clarify and Implement captures also had no issue-title filter, so a newer run from the parallel alt-model smoke job could be taken as ours. All three captures now walk later pages of the same query, still matching only non-skipped runs of that phase whose `display_title` is the smoke issue's title. A missing ID now stops the gate at that phase's line. The check that keeps the Plan step waiting while another Plan run for the issue is still active pages the same way, so an older active run past page 1 no longer ends the step as `plan_failed`. When that check cannot read a page, it retries only until the Plan phase's inactivity limit and then fails with `status=timeout`, instead of polling until the job's 300-minute limit.

| The numbers that matter | Value |
| --- | --- |
| Pages read by each run-ID capture (Clarify, Plan, Implement) | 1 normally, at most 10 per attempt (GitHub's 1,000-result ceiling); a walk that reads all 10 full pages without a match is not retried |
| Pages read by each 10-second Plan status poll | 1 (unchanged) |
| Pages read by the "other active Plan runs" check (Plan completed without its label) | 1 when page 1 holds an active run, otherwise up to 10, stopping at a short page |
| New status value for `wait-clarify`, `wait-plan`, and `wait-implement` | `run_id_missing` |
| Retries of an unreadable runs page in the "other active Plan runs" check | until `PLAN_PHASE_TIMEOUT` (default 60 minutes) without activity, then `status=timeout` |
| Runs created in the first failed window (03:46–03:57 UTC) | 166 |

What this means for release operators: a busy repository no longer turns a successful Clarify, Plan, or Implement phase into a release block with no failed steps, and deep verification checks the smoke issue's own runs rather than the alt-model job's. If a phase's run really cannot be found, the gate prints `FAILED (run_id_missing)` on that phase's line and the wait step names the issue and title it searched for.

### For contributors

The Plan change is in the `wait-plan` step (`fetch_plan_runs_page_json`, `latest_scoped_run_field`, `require_plan_run_id`, `fail_plan_confirm_retry_if_idle`). `wait-clarify` and `wait-implement` keep their own `capture_run_id`, now paged and title-scoped, plus `require_scoped_run_id`, and take `ISSUE_TITLE` from `steps.create-issue.outputs.title`; like `wait-plan`, they fail before polling when that title is empty, because an empty title would match a run with an empty `display_title`. `tests/test_test_and_mark_stable_plan_polling_guard.py` runs the real step scripts against a stubbed `gh` to cover the missing-ID, later-page, other-issue, page-cap, and unreadable-page cases for all three captures, and the empty-title case for `wait-clarify` and `wait-implement`.
