<!-- changelog: changed -->
- **A clean base update gets a delta final CI run instead of the full suite.** After the freshness gate updates an approved PR from its base, and the approved head already passed a full final CI run, the update head's `CI [pr:<n>] [sync-quick]` run now also covers overlaps outside the CI-wiring files (Refs #10097, Phase 2 of `docs/plans/merge-lease-short-critical-section-plan.md`).

The run keeps its three jobs (`static-checks`, `tests-sync-quick`, `lint`). `tests-sync-quick` runs the PR's own tests, the CI-structure tests and, new here, the tests the pre-push selector picks for the files both the PR and the base changed, each file once. It refuses the quick run, and the full run follows as before, when the switch is off, the approved head has no full green run, the selector fails or it picks more than `MERGE_BASE_SYNC_DELTA_MAX_TEST_FILES` test files (default 60). The full suite still runs on the base after every merge.

What this means for operators: expect more `[sync-quick]` final CI runs and fewer full runs on update heads, and `MERGE_BASE_SYNC_FINAL_CI ... reason=delta_overlap sync_quick=true` and `CI_SYNC_QUICK mode=verify outcome=ok reason=delta_overlap` in the logs. The `tests-sync-quick` job timeout is now 30 minutes. Set the repository variable `MERGE_BASE_SYNC_DELTA_CI_ENABLED=false` to go back to the full run; until the review workflows export it, each affected PR first gets one failed `[sync-quick]` run (`reason=delta_disabled`).

### For contributors

New: `scripts/ci_sync_quick.sh delta-supported`, the delta branches of its `verify` and `run-tests`, and the delta arm of `_pr_base_sync_dispatch_final_ci` in `scripts/pr_checks_lib.sh`. `scripts/stage_switches.json` also registers `MERGE_LEASE_READY_GRANT_ENABLED` and `MERGE_LEASE_CONFLICT_RELEASE_ENABLED`, which Phase 1 added to the workflows.
