<!-- changelog: added -->
- **Check-failure triage routes a fix to the failing PR's orchestrator integration branch.**

A triage issue for a failing PR whose head is an `orchestrator/project-<N>` branch of the same repository now carries an `Integration branch:` line. The fix is planned and implemented against that branch instead of the default branch. The branch name comes only from the GitHub API's PR head and must exist. Fork heads, other branch names, a missing branch or a failed lookup leave the line out. The repository variable `TRIAGE_PR_HEAD_BRANCH_METADATA_ENABLED` defaults to `true`. `false` turns it off, and any other value also turns it off and logs a warning.

What this means for operators: triage fixes for integration-branch PRs no longer land on the default branch, and those triage issues no longer stop in planning for want of the branch line. Existing triage issues are not edited or backfilled. A trusted person must add the line to an older open triage issue, or re-file it.
