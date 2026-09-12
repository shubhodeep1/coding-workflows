<!-- changelog: fixed -->
- **Review autofix same-head resume, the review-issue ledger, and validation hints persist across runs again.** The affected `actions/cache` paths no longer embed the per-run workspace directory, so a run can restore what the previous run saved.

Every `review_autofix.yml` run since the workspace-reuse change cached its ledger and partial-resume marker under `${RUNNER_TEMP}/workspaces/<pr>-<run_id>-<run_attempt>/.ai/…`. actions/cache hashes the path list into the cache version and only matches a save whose key and version both agree, so each run computed a new version, logged `Cache not found for input keys: review-ledger-…`, and started from `AUTOFIX_RESUME_ROUND=0`. On PR #4077 (security-pass fix cycle 5 for project #3965) that produced 13 consecutive same-head runs, each ~1.5 h of reviewer and editor spend, each ending with `Resume round: 1 of 3`; the `REVIEW_MAX_RESUME_ROUNDS` bound could never trip. The three ledger cache steps now share one run-independent, repository/PR-scoped path list, and stage steps copy between that directory and the workspace without letting concurrent PRs on one self-hosted runner clobber each other. `validate.yml`'s behavioural-smoke restore uses the identical list, while its validate-hints cache now uses a separate repository/fingerprint-scoped staging path so repeated validation can skip discovery when workspace reuse is off.

| The numbers that matter | Value |
| --- | --- |
| Looping PR | shubhodeep1/coding-workflows#4077 (13 same-head partial-finalize runs) |
| Cache steps aligned | 3 in `review_autofix.yml`, 1 in `validate.yml` |
| Stable staging scopes | Review ledger: repository + PR; validate hints: repository + discovery fingerprint |
| New log lines | `REVIEW_LEDGER_CACHE_STAGE_IN/OUT`, `VALIDATE_HINTS_CACHE_STAGE_IN/OUT` |
| Regression test | `tests/test_review_ledger_cache_path_stability.py` |

What this means for operators: same-head partial-finalize runs now stop after `REVIEW_MAX_RESUME_ROUNDS` (default 3) or on `no_progress`, and the review-issue ledger's `PERSISTING` / `accepted-residual` history carries across autofix iterations again instead of resetting every run.

### For contributors

The stage-in and stage-out steps are inline shell, `continue-on-error: true`, and fail open when `RUNNER_TEMP` or `workspace_path` is unavailable. Keep the ledger path list byte-identical across all four cache steps; the regression test asserts parity, rejects any `workspace_path`, `run_id`, or `run_attempt` reference in cache paths, and round-trips both cache families across distinct workspaces.
