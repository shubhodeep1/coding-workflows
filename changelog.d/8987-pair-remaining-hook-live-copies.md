<!-- changelog: fixed -->
- **The conflict resolver now merges conflicts on every live `.claude/hooks/` file, not just two of them.** `gh_api_write_guard.py`, `pr_watch_guard.py` and `session-start.sh` join `pr_merge_status_guard.py` and `prepush_affected_tests.py` in `PAIRED_LIVE_COPIES` (`scripts/review_untrusted_workspace.py`).

When one of these live hooks and its `workflow-templates/.claude/hooks/` twin conflict with identical index stages, the model resolves the template and the runner copies it byte-for-byte over the live hook. Before this, the live copy was refused as a dot-directory path and the whole conflict set failed closed with `sandbox_path_host_only`, the failure that parked integration PR #8436 for `prepush_affected_tests.py`. A live copy whose template conflicts differently still fails closed. A new test fails when a hook with a template twin is missing from the list.

| The numbers that matter | Value |
| --- | --- |
| Paired live hooks | 5 of 5 in `.claude/hooks/` |
| Newly paired | `gh_api_write_guard.py`, `pr_watch_guard.py`, `session-start.sh` |

What this means for operators: a merge that changes any shipped hook on both sides no longer needs a manual merge.
