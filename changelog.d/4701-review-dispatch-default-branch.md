<!-- changelog: security -->
- **Review dispatches from the poller, merge train, and forward-merge fallback now use the default-branch workflow.** PR numbers are validated before dispatch; no PR head ref selects executable workflow code.

The poller, merge train, and forward-merge fallback pass validated PR numbers to review workflows on the default branch instead of executing workflow files from a PR head. Consumer dispatches show `AI Review [pr:<N>]` in the Actions list, while internal dispatches retain `Internal: AI Review & Autofix [pr:<N>]`. The poller and merge train associate those runs with their PRs and continue to recognize legacy head-branch runs. Active reviews no longer invite redundant dispatches or empty-commit pushes simply because their workflow ran on the default branch.

| The numbers that matter | Value |
| --- | --- |
| Dispatch sites using the default-branch workflow | 3 |
| PR-named lookup on a branch-lookup miss | 2 wrapper listings, paginated up to 10 pages each |
| New API calls for the cached stall-judge and empty-commit scans | 0 |

What this means for operators: default-branch review runs remain visible under their PR-numbered Actions names, and the poller can wait for a pending review without pushing over its work. Incomplete PR-named run listings defer dispatch and empty-commit recovery until the next poll tick.

### For contributors

`_pr_named_review_dispatch_runs <pr> [lookback_minutes]` is the shared paginated lookup on head-branch misses. `_direct_inflight_review_run_on_branch <branch> [pr]` accepts an optional PR number and returns `listing-incomplete` when a lookup cannot rule out an active run; its one-argument branch-only behavior remains unchanged.
