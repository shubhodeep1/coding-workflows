<!-- changelog: changed -->
- **A red default branch now gets one heal issue per failing job, and check-failure triage no longer routes a fix to a branch that lacks the failing files.**

While `main` stayed red, each failed push run of the same CI job could get a new error fingerprint and so a new `ai:workflow-heal` issue, or an escalation once the lineage cap was reached. The heal intake now gives a failed push run on the default branch a red-main group key (workflow, first failing job, branch). While an open heal issue carries that key's header line, later failures of the same job are recorded on it as occurrences, ahead of the lineage cap and the budget caps. `WORKFLOW_HEAL_RED_MAIN_GROUPING_ENABLED=false` (repository variable) turns this off.

When `CHECK_TRIAGE_PR_BRANCH_ROUTING_ENABLED` routes a triage issue to the failing PR's head branch, the collect stage now checks the failing test files the CI log names. If none of them exists at the failing commit but at least one exists on the default branch, the route is dropped and the issue is planned on the default branch. Any lookup failure keeps the route. `CHECK_TRIAGE_TARGET_FILES_GUARD_ENABLED=false` turns this off.

What this means for consumer repos: triage issues under PR-branch routing may now be planned on the default branch when the PR's branch cannot hold the fix. Nothing else changes for consumers.
