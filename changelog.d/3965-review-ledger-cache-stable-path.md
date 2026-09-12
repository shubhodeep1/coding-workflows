<!-- changelog: fixed -->
- **Review autofix same-head resume and the review-issue ledger persist across runs again.** The `actions/cache` path list in `review_autofix.yml` no longer embeds the per-run workspace directory, so a run can restore what the previous run saved.

Every `review_autofix.yml` run since the workspace-reuse change cached its ledger and partial-resume marker under `${RUNNER_TEMP}/workspaces/<pr>-<run_id>-<run_attempt>/.ai/…`. actions/cache hashes the path list into the cache version and only matches a save whose key and version both agree, so each run computed a new version, logged `Cache not found for input keys: review-ledger-…`, and started from `AUTOFIX_RESUME_ROUND=0`. On PR #4077 (security-pass fix cycle 5 for project #3965) that produced 13 consecutive same-head runs, each ~1.5 h of reviewer and editor spend, each ending with `Resume round: 1 of 3`; the `REVIEW_MAX_RESUME_ROUNDS` bound could never trip. The three cache steps now share one run-independent path list under `${RUNNER_TEMP}/review-ledger-cache/`, and new `Stage restored review-issue ledger into workspace` / `Stage review-issue ledger for … cache save` steps copy between that staging dir and the workspace. `validate.yml`'s `Restore behavioural smoke runtime cache` uses the identical list so it can read the review run's `judge_interim.json` priors.

| The numbers that matter | Value |
| --- | --- |
| Looping PR | shubhodeep1/coding-workflows#4077 (13 same-head partial-finalize runs) |
| Cache steps aligned | 3 in `review_autofix.yml`, 1 in `validate.yml` |
| New log lines | `REVIEW_LEDGER_CACHE_STAGE_IN restored_entries=<n>`, `REVIEW_LEDGER_CACHE_STAGE_OUT staged_entries=<n>` |
| Regression test | `tests/test_review_ledger_cache_path_stability.py` |

What this means for operators: same-head partial-finalize runs now stop after `REVIEW_MAX_RESUME_ROUNDS` (default 3) or on `no_progress`, and the review-issue ledger's `PERSISTING` / `accepted-residual` history carries across autofix iterations again instead of resetting every run.

### For contributors

The stage-in and stage-out steps are inline shell, `continue-on-error: true`, and fail open when `RUNNER_TEMP` is unset or the staging dir is empty. Keep the path list byte-identical across all four cache steps; the regression test asserts parity and rejects any `workspace_path`, `run_id`, or `run_attempt` reference in it.
