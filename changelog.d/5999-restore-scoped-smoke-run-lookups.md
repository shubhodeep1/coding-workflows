<!-- changelog: fixed -->
- **The release smoke test scopes its Clarify, Plan and Implement run lookups to its own issue again.** The stable → main forward merge #5892 had dropped those lookups in `test-and-mark-stable.yml`, and `main`'s CI failed from 2026-10-01 08:59 UTC until this fix.

The conflict resolver of #5892 kept `stable`'s side of every conflicting hunk in `.github/workflows/test-and-mark-stable.yml` and removed `main`'s three `find_latest_scoped_run_field` call sites. The workflow now calls the shared helper again, keeping `stable`'s 10-page cap, `require_plan_run_id` and `run_id_missing` exits. The helper in `scripts/comprehensive_test_and_release_gh_api.sh` gains an optional page cap that returns exit 2 when every page is full, and returns exit 1 for a page that is not a runs listing. Callers that pass no cap behave as before.

What this means for operators: PRs into `main` that failed `tests-release-and-log-analysis` and `lint` on `tests/test_release_smoke_run_lookup.py` since 08:59 UTC go green again after a `main` merge. An empty smoke-issue title still ends the wait steps with `run_id_missing`.
