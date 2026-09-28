<!-- changelog: fixed -->
- **The release smoke test no longer reports Plan success without a Plan run ID.** `Phase 2: Wait for plan to complete` in `test-and-mark-stable.yml` now finds its Plan run past the first 100 runs, and when no run can be found it fails at Plan capture with `status=run_id_missing`.

Run 36374918973 blocked the stable release at `Internals .. FAILED` even though its totals showed 0 failed steps. The Plan wait step read one 100-run page of `actions/runs`. As skipped `issue_comment` runs piled up, the smoke issue's Plan run slid to index 96 of that page and then off it, so the step wrote `status=success` with an empty `run_id`. That skipped `Phase 2b: Soft-error analyser (plan)` and left deep verification to fail the release as `Plan: run ID not found`. The lookup now walks later pages of the same query, still matching only non-skipped runs whose name contains `Plan` and whose `display_title` is the smoke issue's title. A missing ID now stops the gate at its Plan line. The check that keeps the step waiting while another Plan run for the issue is still active pages the same way, so an older active run past page 1 no longer ends the step as `plan_failed`.

| The numbers that matter | Value |
| --- | --- |
| Pages read by the Plan run-ID capture | 1 normally, at most 10 per attempt (GitHub's 1,000-result ceiling); a walk that reads all 10 full pages without a match is not retried |
| Pages read by each 10-second status poll | 1 (unchanged) |
| Pages read by the "other active Plan runs" check (Plan completed without its label) | 1 when page 1 holds an active run, otherwise up to 10, stopping at a short page |
| New `wait-plan` status value | `run_id_missing` |
| Runs created in the failed window (03:46–03:57 UTC) | 166 |

What this means for release operators: a busy repository no longer turns a successful Plan phase into a release block with no failed steps. If the Plan run really cannot be found, the gate prints `Plan ....... FAILED (run_id_missing)` and the `wait-plan` step names the issue and title it searched for.

### For contributors

The change is confined to the `wait-plan` step (`fetch_plan_runs_page_json`, `latest_scoped_run_field`, `require_plan_run_id`). Clarify and Implement run-ID capture still read one page. `tests/test_test_and_mark_stable_plan_polling_guard.py` now runs the real step script against a stubbed `gh` to cover the missing-ID, page-2, other-issue, and page-cap cases.
