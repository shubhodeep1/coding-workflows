<!-- changelog: security -->
- **Consumer repos can no longer change the coding-workflows managed workflow wrappers.** A new `ai-workflow-integrity.yml` check, a sync that ignores the old opt-out, and a daily restore pull request keep every wrapper equal to the signed release.

Every install profile now ships `.github/workflows/ai-workflow-integrity.yml`. It runs on `pull_request_target` and on pushes, and fails any change to a managed wrapper (including `ai-update-workflows.yml` and itself) unless all managed wrappers are byte-identical to the templates of the signed release they pin; the verdict is the commit status `coding-workflows/workflow-integrity`. `ALLOW_WORKFLOW_EDITS=false` and `allow_workflow_edits: false` no longer stop wrapper updates: the sync logs `WORKFLOW_EDITS_OPT_OUT` and a deprecation warning, and still skips only the non-wrapper assets. The consumer drift audit runs daily instead of weekly, compares against the released templates, and opens or refreshes one restore pull request (`auto/restore-managed-workflows`) in each drifted consumer while keeping its Telegram alert. With `AI_PIPELINE_APPROVER_LOGIN` set, the repository owner's own account may again post the `[auto-answered-by-*]` and `[auto-approved-by-plan]` markers, since the pipeline posts them with `GH_PAT`.

| The numbers that matter | Value |
| --- | --- |
| Required status context | `coding-workflows/workflow-integrity` |
| Drift audit schedule | daily, 08:00 UTC (was Mondays) |
| Restore branch | `auto/restore-managed-workflows` |
| New log prefixes | `WORKFLOW_INTEGRITY`, `WORKFLOW_EDITS_OPT_OUT`, `DRIFT_REPAIR` |
| New repo variable (this repo) | `CONSUMER_DRIFT_REPAIR_ENABLED` (default on) |

What this means for consumer repos: merge the sync pull request that adds `ai-workflow-integrity.yml`, remove any `allow_workflow_edits: false` from `ai-update-workflows.yml`, and move wrapper changes upstream. Once the default branch has the check, a branch ruleset requires it; repository admins can still bypass.

### For contributors

The integrity checker and the copy of the sync-manifest verifier live inline in `.github/workflows/workflow_integrity.yml` (consumers pin that file, so it never imports code from the tree it judges); `tests/test_workflow_integrity.py` pins the verifier copy byte for byte. The sync's wrapper compare is now an exact `cmp -s`; the previous `diff --ignore-space-at-eol` is not a GNU diff option and reported every wrapper as updated on every run.
