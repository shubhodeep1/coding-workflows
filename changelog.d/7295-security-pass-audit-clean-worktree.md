<!-- changelog: fixed -->
- **The project security pass now audits the integration head's own files.** In this repository the poller's checkout of the integration branch can carry the default branch's copies of `scripts/` and `prompts/`, restored over the tree after a forced checkout. The audit read those copies while its diff came from the integration branch, so project #6664 kept re-reporting two findings its branch had already fixed.

`run_security_pass_inline` (`scripts/orchestrate_poll_process.sh`) now checks the audited head out into a clean detached worktree under `RUNTIME_DIR` and runs the audit there. The exhaustion judge reads the same worktree. Every script that runs still comes from the poller's trusted support root, so no branch-written code runs on the runner. The false-positive exclusion catalog also comes from the support root, never from the audited branch. The worktree must match the audited head and be clean; otherwise the pass fails closed (`engine_unavailable`). The worktree is removed when the pass returns. `SECURITY_PASS_AUDIT_CLEAN_WORKTREE_ENABLED=false` turns this off (default `true`). Logs `SECURITY_PASS_AUDIT_TREE`.

| The numbers that matter | Value |
| --- | --- |
| Extra GitHub API calls | 0 |
| Extra local worktrees per audit | 1 |
