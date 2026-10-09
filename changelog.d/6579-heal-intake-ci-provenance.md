<!-- changelog: fixed -->
- **A failed `CI` run on `main` now opens a heal issue.** The heal intake's run check accepted only release workflows, so it dropped every failed `CI` run as `unexpected_workflow_path` before reading its logs.

`scripts/workflow_failure_heal.py` now accepts `ci.yml` runs in `workflow_run` reports, but only when GitHub reports the run as a `push` to the repository's default branch. Any other CI run is rejected as `ci_not_default_branch_push`. The intake reads the default branch from `WORKFLOW_HEAL_DEFAULT_BRANCH`, or from its own event's `repository.default_branch`, with no extra GitHub API call. If neither is available, CI runs are rejected and release runs are unaffected.
