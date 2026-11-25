<!-- changelog: fixed -->
- **A final CI run cut off under a branch's older `ci.yml` time limit now gets a base sync instead of a review round.**

A final CI run (`CI [pr:<n>]`) is dispatched on the PR head ref, so it uses the branch's own `.github/workflows/ci.yml`, including its `timeout-minutes`. After `main` raised the `orchestrate-poll` limit from 20 to 30 minutes (#9898), PR #9832, 84 commits behind, had its final run cut off at 20 minutes with no failed test (run 45520845433). The hand-back sent it to review, where no change in the PR could fix it.

`scripts/ci_cancelled_rerun.py classify-run` now returns `stale_workflow_timeout` for such a run: every job `lint` names was cancelled with the timeout annotation and no failed step, `lint` failed only at its require step, and `ci.yml` on the head differs from the base's copy (with a higher base limit for that job when both parse). `scripts/review_final_ci_failed.sh` then posts an `ai:final-ci-stale-workflow:v1` marker and logs `outcome=stale_workflow_sync_deferred` instead of handing back. The review sweep's base sync updates the branch once, within `FINAL_CI_SWEEP_BASE_SYNC_MAX`, and the new head runs its final CI under the current workflow file. When the sweep cannot sync the head, it hands it back to review as before.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls per qualifying failed run | 2 contents reads, 1 comment POST |
| Extra API calls in the sweep | 0 (reuses its comments read); the hand-back's own calls only when a marked head cannot sync |
| Syncs per head | at most 1, within `FINAL_CI_SWEEP_BASE_SYNC_MAX` |
| New repo variables | `FINAL_CI_STALE_WORKFLOW_SYNC_ENABLED` (default `true`) |
| New jobs or workflows | none |

What this means for operators: old branches no longer go back to review when `main` raised a job's time limit after they were cut. Set `FINAL_CI_STALE_WORKFLOW_SYNC_ENABLED=false` to hand every such run back to review as before.
