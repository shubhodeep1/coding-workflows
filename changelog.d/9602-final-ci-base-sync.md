<!-- changelog: fixed -->
- **A reviewed PR whose final CI run failed because of its base now gets the current base and a fresh run, instead of staying red.** Final CI also stops testing a merge ref that GitHub has not refreshed after the base moved.

Five open PRs (#9454, #9562, #9602, #9614, #9621) passed review and then sat red on a final `lint` failure that their own diffs did not cause, and nothing ran CI on them again. A head that is far behind runs its own old `ci.yml`. A stale base still fires the retired `pull_request` trigger, which never reaches the `final-ci-failed` hand-back. And #9602's run tested a merge with a base commit that the base had fixed two hours earlier. Now the review sweep (`review_autofix_sweep.yml`, `scripts/review_final_ci_sweep.sh`) updates such a head from its base. It applies to a head that passed review, failed every completed CI run and is behind its base, once the failure is 30 minutes old. The sync is skipped while the base's own push CI is red. It uses the merge path's verified update and `ai:merge-base-sync:v1` marker, so the update commit only waits for CI. Its final run is dispatched right away. In `ci.yml`, every job of a final run now checks that `refs/pull/<n>/merge` merges into the base's current tip. It refreshes a stale ref, or fails the job with `CI_FINAL_MERGE_REF` when GitHub cannot refresh it.

| The numbers that matter | Value |
| --- | --- |
| Minimum age of the failure before a sync | 30 minutes (`FINAL_CI_SWEEP_BASE_SYNC_MIN_AGE_MINUTES`) |
| Sweep syncs per PR | at most 3 (`FINAL_CI_SWEEP_BASE_SYNC_MAX`) |
| Candidates per sweep tick | `FINAL_CI_SWEEP_MAX_PER_TICK` (default 10) |
| Merge-ref refresh | up to 6 attempts, 10 seconds apart, in each of the 10 final-CI checkouts |
| Switches | `FINAL_CI_SWEEP_BASE_SYNC_ENABLED`, `CI_FINAL_MERGE_REF_FRESH_ENABLED` (both default `true`) |

What this means for operators: a PR that failed final CI only because its base was broken or had moved now merges once the base is green, with no manual branch update. A PR whose own code is broken still goes back to review as before, and is re-synced at most three times while its base moves. A final CI job that fails with `CI_FINAL_MERGE_REF ... outcome=stale` or `outcome=conflict` points at the merge ref, not at a test.

### For contributors

`_pr_ensure_final_ci` now exports `PR_FINAL_CI_REASON` and `PR_FINAL_CI_FAILED_AT`. `_pr_base_sync_count` also sets `PR_BASE_SYNC_FINAL_CI_COUNT`. `_pr_base_sync_marker_post` takes an optional `PR_BASE_SYNC_MARKER_LEAD`, and the merge-path marker text is unchanged. Tests: `tests/test_review_final_ci.py`, `tests/test_ci_final_merge_ref_fresh.py`.
