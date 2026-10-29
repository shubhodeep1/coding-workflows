<!-- changelog: fixed -->
- **Workflow failure heal now accepts a self-dispatched `review_autofix.yml` run that the pipeline's own failure marker binds to the PR head.** Before this, the intake dropped the run as a head mismatch and paged as an intake defect.

`review_autofix.yml` re-dispatches itself on the default branch with no `run-name`, so the run's title has no `[pr:N]` and its `head_sha` is the branch tip, not the pull request head that an `autofix_failure` report carries. The provenance gate already linked such a run to the PR through the trusted `review-autofix-failure:v1` marker the pipeline posts on the PR. The evidence check in `scripts/workflow_failure_heal_intake.sh` still required the run head to equal the PR head (the #7425 fix only covered `[pr:N]` titles), so it dropped the only run, the scope marker could not be rendered, and the intake raised two CRITICAL alerts instead of filing a heal issue. `verify-run-provenance` now lists, in `marker_head_run_ids`, every run that a trusted failure or cap marker names together with the report's PR head, and `verify-run --marker-head-bound` accepts a `workflow_dispatch` run on that list. A marker for an older head, or one written by anyone but the pipeline account, binds nothing, and every other head mismatch is still rejected.

| The numbers that matter | Value |
| --- | --- |
| Report that hit this | `coding-workflows` PR 8285, review run 40811558217, intake run 40814586379 (2026-10-29) |
| CRITICAL alerts per affected report | 2 |

What this means for operators: a failed self-dispatched review now becomes a heal issue rather than an intake-defect page.
