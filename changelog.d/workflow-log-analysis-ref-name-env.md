<!-- changelog: fixed -->
- **Workflow log analysis no longer puts the branch name into its shell scripts.** The report commit step and the Telegram report link in `workflow-log-analysis.yml` now read the branch from the runner's `GITHUB_REF_NAME` variable.

Before, both steps used `${{ github.ref_name }}`, which GitHub writes into the script text before bash runs it, so a crafted branch name could run shell code in a step that holds the pipeline token (finding SEC-001 in `analysis/workflow-optimization-2026-09-23-2.md`). The branch name is now read as data. An empty value stops the report commit step with the same error as a non-branch ref.
