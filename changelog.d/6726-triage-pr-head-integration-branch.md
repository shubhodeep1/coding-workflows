<!-- changelog: added -->
- **Check-failure triage can route a fix to the failing PR's orchestrator integration branch.**

With the new repository variable `TRIAGE_PR_HEAD_BRANCH_METADATA_ENABLED` set to `true`, a triage issue for a failing PR whose head is an `orchestrator/project-<N>` branch of the same repository carries an `Integration branch:` line, so the fix is planned and implemented against that branch instead of the default branch. The branch name comes only from the GitHub API's PR head and must exist; fork heads, other branch names, a missing branch or a failed lookup leave the line out. The variable defaults to `false`, so nothing changes until an operator turns it on.

What this means for operators: once the operator step for #6710 is done, set `TRIAGE_PR_HEAD_BRANCH_METADATA_ENABLED=true` to stop triage fixes for integration-branch PRs from landing on the default branch.
