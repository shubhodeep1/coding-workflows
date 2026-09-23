<!-- changelog: fixed -->
- **Workflow failure heal now fixes a failing coding-workflows pull request on that pull request's own branch, instead of opening a `stable` hotfix.** A repeated review/autofix failure on a PR in this repo files its heal issue with `Target branch:` set to the PR's head branch.

In coding-workflows, `review_autofix.yml` resolves `SCRIPT_REF` to `github.sha`, so a review run executes the pull request's own workflow code. When that code breaks the run, the defect may not exist on `stable`. Heal issue #4329 hit this: PR #4323 added an unguarded workspace-guard sandbox call that exited with systemd status 226 on all five review runs. The heal was still filed with `Target branch: stable`, and its fix PR #4332 opened against `stable` carried 100 files of unreleased `orchestrator/project-4139` work. `scripts/workflow_failure_heal_intake.sh` now targets the PR's head branch for a self-repo `autofix_failure` report, falls back to `stable` (`warn source_pr_branch_missing`) when that branch is gone, and adds `target_branch_source=default|failed_run_branch|source_pr_head` to the `WORKFLOW_HEAL created` log line.

| The numbers that matter | Value |
| --- | --- |
| Review runs lost on PR #4323 before the heal fix could land | 5 (exit code 226) |
| Files in heal PR #4332 against `stable` vs against the PR branch | 100 vs 11 |
| Extra GitHub API calls | none when the PR branch exists; one more branch lookup when it is gone |

What this means for operators: a heal fix for a coding-workflows PR now opens against that PR's branch, so it unblocks the PR directly and reaches `main` and `stable` through the PR's normal merge path. Consumer reports and failed release runs keep their current targets, `WORKFLOW_HEAL_TARGET_BRANCH` and the failed run's branch.
