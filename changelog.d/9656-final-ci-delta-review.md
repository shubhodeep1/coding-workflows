<!-- changelog: added -->
- **A final-CI repair commit is now reviewed on its own diff.** The reviewer panel sees only the repair, not the whole pull request it already approved.

When the final-CI repair path commits `[ai-merge-resolve] repair final CI failures` on top of an approved head, the review of that new head used the PR's full merge-base diff, so the panel re-reviewed code it had already passed. The review gate now recognises a repair head with `scripts/review_head_gate.sh final-ci-repair`: a pipeline `ai:final-ci-repair:v1` marker names it, its single parent is the approved head, its committer is the pipeline's resolver identity, and the approved head carries a pipeline-posted `ai-review/head-gate` success. The "Generate diff context" step then makes `<approved head>..HEAD` the reviewers' focus diff, the same slot that normally holds the previous autofix commit, and logs `REVIEW_DIFF_CONTEXT range=delta`. The whole-PR diff stays available as context, so the reviewer risk tier, panel, editor and head-gate status work as before, and the repair head still gets its own full final CI run before merge.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls per repair head | 2 (`GET commits/<head>`, `GET commits/<from>/statuses`) |
| Extra API calls for any other head | 0 |
| New repo variable | `FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED` (default `true`) |

What this means for operators: repair heads get a short review of the fix itself. Any check that fails or cannot be read falls back to the full review. Set `FINAL_CI_REPAIR_DELTA_REVIEW_ENABLED=false` to review repair heads in full.
