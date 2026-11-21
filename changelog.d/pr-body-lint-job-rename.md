<!-- changelog: fixed -->
- **The PR-body auto-close lint no longer reports a check named `lint`.** Its job in `.github/workflows/lint-pr-body-auto-close.yml` is now `pr-body-lint`, so the `lint` check the `main` ruleset requires can only come from the full CI run (or from `lint-plan-archival.yml` on a PR that changes files under `docs/completed/`).

The `main` ruleset requires two contexts, `lint` and `ai-review/head-gate`. `lint` is meant to be the aggregate job of `.github/workflows/ci.yml`, but the PR-body lint job had the same name and GitHub accepted either. On PR #9570 the PR-body check passed about six hours before CI's `lint` did, so GitHub's auto-merge and the merge button treated the requirement as met without a CI result. The pipeline's own review merge paths were not affected: `_pr_wait_for_required_checks` already binds `lint` to a CI run. A failed PR-body lint keeps blocking the pipeline's merges, because `pr-body-lint` is now part of `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT`.

| The numbers that matter | Value |
| --- | --- |
| Old check-run name | `lint` |
| New check-run name | `pr-body-lint` |
| Workflow name and file | unchanged |
| `ORCH_FINAL_MERGE_REQUIRED_CHECKS` default | adds `pr-body-lint` |
| Jobs per PR event | unchanged (1 for this workflow) |

What this means for operators: a PR with auto-merge enabled now waits for the final CI run's `lint` instead of merging on the PR-body check. The ruleset does not require `pr-body-lint`; add it there if GitHub's own merge paths should wait for the §19 check too, after open PRs have picked up the new name (a branch that predates the rename reports `lint` until its next `pull_request` event). If `ORCH_FINAL_MERGE_REQUIRED_CHECKS` is set explicitly anywhere, add `pr-body-lint` to that value.

### For contributors

`lint-plan-archival.yml` still has a job named `lint`. It was left alone: the rename approved here covers the PR-body job only.
