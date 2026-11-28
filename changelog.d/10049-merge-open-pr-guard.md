<!-- changelog: fixed -->
- **Review runs no longer merge a pull request that was closed while they waited.** Refs #10260 (re-issued from #10049).

  - Every review merge path re-reads the PR before it merges and goes ahead only when the PR is open, is not a draft and is still at the reviewed head. This covers the deterministic-skip merge job, the auto-merge step, the review-blocked judge and the final-CI resume tick. The check runs once when the merge path starts and once right before the merge. A closed PR gives back the merge lease, gets no merge labels, and the job logs `AUTOFIX_MERGE_SKIPPED ... reason=pr_closed closed_at=<iso>`. When the state cannot be read, nothing is merged (`reason=pr_state_unavailable`).
  - AI Cancel on PR Close now also cancels the closed PR's runs that GitHub no longer links to it (same head branch and repository, started after the PR was opened and before it was closed). That includes a review run whose merge job is still queued. Runs the close itself starts, and every unlinked run on a merged close, are left alone.
  - Switch: `PR_MERGE_OPEN_GUARD_ENABLED` (default `true`).
