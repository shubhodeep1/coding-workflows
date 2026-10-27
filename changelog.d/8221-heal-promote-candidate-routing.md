<!-- changelog: fixed -->
- **Workflow-failure heal issues for a failed release run on `promote-candidate` now target `main`.** Before, they targeted `promote-candidate` itself, and the resulting PR could never merge.

A release gate run on the pipeline-only `promote-candidate` mirror failed, and `scripts/workflow_failure_heal_intake.sh` filed heal issue #8217 with `Target branch: promote-candidate`. Implement opened PR #8221 against that branch. The "promote-candidate pipeline-only" ruleset blocks every update that does not come from the pipeline, so each review-blocked judge "merge" decision ended in `merge_failed` (auto-merge: "Branch does not have required protected branch rules"; direct merge: "the base branch policy prohibits the merge"), and a CRITICAL alert fired every 30 minutes. The intake now routes these issues to the repository's default branch, which `promote-candidate-follow.yml` fast-forwards the mirror to.

| The numbers that matter | Value |
| --- | --- |
| Judge `merge` decisions that failed on PR #8221 | 5 (18:48 to 20:41 UTC, 2026-10-27) |
| New log line | `target_branch_retarget from=promote-candidate to=<default> reason=pipeline_mirror_branch` |
| New `target_branch_source` value | `pipeline_mirror_branch` |

What this means for operators: a failed release gate on `promote-candidate` now gets a heal PR against `main` that can merge normally. Heal issues already filed against `promote-candidate` (#8217) keep their old target and need their PR retargeted by hand.
