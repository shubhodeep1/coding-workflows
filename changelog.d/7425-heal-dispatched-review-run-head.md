<!-- changelog: fixed -->
- **Workflow failure heal now accepts review runs that were dispatched for a pull request.** Before this, the intake rejected them as a head mismatch and paged as an intake defect.

A review started with `workflow_dispatch` (`AI Review [pr:N]`) runs on the default branch, so its `head_sha` is the branch tip, not the pull request head that an `autofix_failure` report carries. The provenance gate already linked such a run to the PR through its `[pr:N]` title. The evidence check in `scripts/workflow_failure_heal_intake.sh` still required the run head to equal the PR head, so it dropped the only run. With no runs left, the scope marker could not be rendered, and every such report raised two CRITICAL alerts ("refused to file an issue without a valid intake-authored scope marker" and "Workflow Failure Heal Intake workflow itself failed") instead of filing a heal issue. `verify-run` now takes `--pr-number`. For an `autofix_failure` report it accepts a `workflow_dispatch` run whose title names that PR, the same rule the provenance gate uses. Every other head mismatch is still rejected.

| The numbers that matter | Value |
| --- | --- |
| Reports that hit this on 2026-10-15 | 2 (`tele-funtoken-msg-scoring` #4584 and #4577, intake runs 38686057824 and 38686754997) |
| CRITICAL alerts per affected report | 2 |

What this means for operators: a dispatched review that fails now becomes a heal issue rather than an intake-defect page. When the intake does drop a run, `run_ref_rejected` now carries a `detail=` token with the reason, for example `detail=error:_run_head_mismatch`.
