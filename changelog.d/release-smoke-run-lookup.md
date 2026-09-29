<!-- changelog: fixed -->
- **The release smoke gate no longer fails a healthy release because it cannot find its own runs.** `test-and-mark-stable.yml` now finds the Clarify, Plan and Implement runs past the first 100 workflow runs, and Phase 7 survives a single transient GitHub error.

Three of the four release-gate runs on 2026-09-28 and 2026-09-29 failed in the E2E smoke test while every pipeline phase passed. In runs 36374918973 and 36504041362, deep verify reported "Plan: run ID not found" and "Implement: run ID not found": the lookup read one page of 100 runs from the repository's whole run list, and the release window created 166 runs in 11 minutes, so the real run fell off the page behind newer `skipped` runs for the same issue. In run 36389883126, Phase 7 exited `lookup_failed` on one HTTP 502 while the cancel-on-close run it was polling was already in progress. The run lookup now reads further pages until it finds a match, and each Phase 7 run listing retries up to 3 times, 5 seconds apart.

| The numbers that matter | Value |
| --- | --- |
| Run lookup pages read | 1 when page 1 matches, at most 5 (500 runs) |
| Phase 7 listing attempts before `lookup_failed` | 3, 5 s apart |
| Failed gate runs traced to these two bugs | 36374918973, 36389883126, 36504041362 |

What this means for operators: a `@stable` promotion or the daily promote cycle should no longer need a manual re-dispatch of `test-and-mark-stable.yml` after a busy release window or a single GitHub 5xx. A real phase failure still blocks the release as before.

### For contributors

The paged lookup is `find_latest_scoped_run_field` in `scripts/comprehensive_test_and_release_gh_api.sh`, used by the three `capture_run_id` definitions and the Plan wait's `latest_scoped_run_field`. The Phase 7 retry is the step-local `phase7_list_cancel_runs`. `tests/test_release_smoke_run_lookup.py` covers both and runs in its own `ci.yml` step.
