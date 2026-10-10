<!-- agents: section="Workflow architecture" -->
Retired host-only conflict re-issue (issue #6680): before stall recovery
dispatches the conflict resolver or the review-blocked judge for an open PR
(managed `resolve_merge_conflict`, the managed open-PR guard, the standalone
conflict guard, and the managed and standalone `dispatch_rb_judge` rungs), it
runs `_stall_retired_host_only_conflict_check` (`scripts/orchestrate_poll_process.sh`,
classifier `orchestrate_lib.py retired-conflict-check`). When every host-only
conflicted path of the PR (one `review_untrusted_workspace.allowed()` refuses)
is listed in the verified support checkout's `workflow-templates/retired_files.txt`
and absent from the base, it runs `close_and_reissue` instead of re-dispatching
a resolver that can only fail closed. The replacement issue names the retired
paths and the closed PR's changed files, and the PR branch is kept. The managed
review-blocked pre-judge dispatch skips the resolver and the judge for such a PR
and leaves the re-issue to the stall-recovery rung. Conflicts are read locally
with `git merge-tree`, the head tip must equal the PR head SHA, and the PR must
be the issue's own implementation PR; any other case, including probe or trust
failures, keeps the existing dispatch, and the resolver's own
`sandbox_path_host_only` check is unchanged. Kill switch
`STALL_RETIRED_CONFLICT_REISSUE_ENABLED` (default `true`).

<!-- agents: section="Stable log prefixes (contractual)" -->
- `STALL_RETIRED_CONFLICT_REISSUE` (`scripts/orchestrate_poll_process.sh`: `issue= pr= outcome=reissue|skip reason= retired_paths=`)
LOG_PREFIX.name=STALL_RETIRED_CONFLICT_REISSUE
