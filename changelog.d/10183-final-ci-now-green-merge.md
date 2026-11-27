<!-- changelog: fixed -->
- **An approved head whose failed final CI turned green now goes on to merge instead of stalling.**

Issue #10023 made the review gate end a run when a head's `ci/final` status turned green after an `ai:final-ci-failed:v1` hand-back (`skip_reason=final_ci_now_green`). That ending also switched off every merge step of the run, and every later run for the same head ended the same way. In PR #10021 the head was green and unmerged for 16 hours, and when it was granted the merge lease it held the slot with nothing to merge it.

The gate still runs no repair and no reviewer panel for such a head, and still logs `FINAL_CI_REPAIR ... outcome=skipped reason=final_ci_now_green`. It now also routes the head to the existing `deterministic-skip-merge` job (`deterministic_skip=true`, `det_skip_reason=final_ci_now_green`). That job runs the base-freshness check (a branch update, then a final CI, when the base moved under the PR's files), the merge lease, the required-checks wait and the squash auto-merge, as for any other skip-path head. A head is not routed, and the run ends as before, when the PR has `force-review`, is a no-PR claude-branch review, is an unmanaged `ai-sync/stable` head, has a merge conflict, or needs the single-issue security pass that the skip job cannot run (`AUTOFIX_SKIP_RUNS_SECURITY_PASS` not `true`, or `ai:security-pass-failed`). A generated chore PR needs no security pass, as on the doc-only / small-diff path, so it is routed without one. Each run logs `FINAL_CI_NOW_GREEN_MERGE ... outcome=routed` or `... outcome=not_routed reason=<token>`.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls in the gate | 0 |
| New jobs or workflows | none |
| New repo variable | `FINAL_CI_NOW_GREEN_MERGE_ENABLED` (default `true`) |

What this means for operators: such a PR merges on the next review run or lease grant, and gets the `ai:review-skipped` label the skip path applies (as `freshness_sync` heads do). To go back to the #10023 behaviour, set `FINAL_CI_NOW_GREEN_MERGE_ENABLED=false`; any value other than `true` or `false` does the same with a warning.
