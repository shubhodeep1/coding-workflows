<!-- changelog: fixed -->
- **A failed final CI run now gets a review, and one that failed for an infrastructure reason gets a re-run instead.** Approved PR heads whose final CI went red were left red: the review the failure dispatched skipped as a clean or terminal same head.

When `ci.yml`'s final run (`CI [pr:<n>]`) fails, `scripts/review_final_ci_failed.sh` posts an `ai:final-ci-failed:v1` marker and dispatches review. The review gate's clean and terminal same-head skips did not look at that marker, so the dispatched run logged `AUTOFIX_GATE_SKIP reason=clean_same_head` and nothing fixed the head (#9239 collected 40 head-gate statuses on one red head; #9602 and #9562 are current examples). The gate now runs one review when the marker is at or after the skip marker. Before posting the marker, the hand-back now classifies the run: a runner shutdown, action-resolution, checkout-fetch or abandoned-job failure posts nothing (`outcome=infra_rerun_deferred`) and the review sweep re-runs the failed jobs once. The sweep's CI re-run step also works again: since final CI moved to `workflow_dispatch`, every tick skipped every PR as `invalid_listing` (58 of 58 PRs at the 2026-11-17 12:00 tick).

| The numbers that matter | Value |
| --- | --- |
| Sweep CI re-run decisions at the 2026-11-17 12:00 tick | 58 of 58 `reason=invalid_listing` |
| Reviews per final-CI failure (infra cause) | 0, plus one failed-jobs re-run |
| Reviews per final-CI failure (real failure) | 1 (was 0, a same-head skip) |
| Extra API reads per failed final run | 1 jobs read and at most 10 annotation reads |
| New repo variable | `FINAL_CI_FAILED_INFRA_RERUN_ENABLED` (default `true`) |

What this means for operators: red approved heads go back through review and autofix once, and runner hiccups in final CI fix themselves on the next sweep tick. Set `FINAL_CI_FAILED_INFRA_RERUN_ENABLED=false` to hand every failure back to review.

### For contributors

`scripts/ci_cancelled_rerun.py` gains `classify-run --repo <o/r> --run-id <id> [--ignore-job <name>]`, and its decision lines for final-CI candidates end in `final_ci=true`. The gate trusts a final-CI marker from the `GH_PAT` login or from `github-actions[bot]`, because the `final-ci-failed` job posts with the run's token; a forged marker can only add one review.
