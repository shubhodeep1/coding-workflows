<!-- agents: section="Workflow architecture" -->
- Workflow-failure heal routing for the promote-cycle mirror (#8217 / PR #8221):
  a `workflow-defect` / `inconclusive` heal issue for a failed release run
  normally targets the branch the run failed on (`target_branch_source=failed_run_branch`).
  A run on `promote-candidate` (`HEAL_PIPELINE_MIRROR_BRANCH` in
  `scripts/workflow_failure_heal_intake.sh`) is the exception: the
  "promote-candidate pipeline-only" ruleset blocks every non-pipeline update,
  so a PR based on it can never merge and the review-blocked judge reports
  `merge_failed` on every cycle. The intake logs
  `target_branch_retarget from=promote-candidate to=<default> reason=pipeline_mirror_branch`
  and files the issue against the default branch (`target_branch_source=pipeline_mirror_branch`),
  which `promote-candidate-follow.yml` then fast-forwards the mirror to. The
  `consumer-app-defect` arm, which files a release run's issue in this repo,
  applies the same retarget and logs the same `target_branch_retarget` line.
