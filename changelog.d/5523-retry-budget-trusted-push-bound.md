<!-- changelog: security -->
- **A PR author can no longer backdate a commit to suppress the editor-changes-lost recovery retry.** This closes security finding #5523 (`retry-budget-uses-author-controlled-commit-time`, medium).

When a review run's editor loses its changes, `review_autofix.yml` dispatches one fresh review of the same head from the default branch. `autofix_changes_lost_head_retry_consumed` in `scripts/gh_helpers.sh` allows only one such retry per head. To find the retry, it counts completed review runs named for the PR that were created after the head was pushed. That push time was the earlier of the head commit's committer time and the head's first workflow run. A PR author sets the committer time freely, so a commit dated days earlier counted every earlier head's review as this head's retry and skipped the recovery dispatch. The bound now uses only the `created_at` of runs GitHub recorded for that exact head SHA, taken from the branch page the helper already reads. When no run on the head is visible, the budget fails closed (`AUTOFIX_CHANGES_LOST_BUDGET_QUERY_FAILED … reason=missing_head_time`) and no retry is dispatched, which is the behaviour before #4898.

| The numbers that matter | Value |
| --- | --- |
| Author-controlled inputs to the retry budget | 0 (was: 1, the committer time) |
| New GitHub API calls | 0 |
| Retries allowed per head | 1 (unchanged) |

What this means for operators: nothing changes in the normal path. A push creates workflow runs on its head, and the bound is the first of them. `AUTOFIX_CHANGES_LOST_BUDGET` log lines keep their fields. A `reason=missing_head_time` skip now also appears when the head's runs are missing from the branch's newest 30 runs, where it used to fall back to the commit time.

### For contributors

The helper's fifth argument and `REVIEWED_HEAD_COMMIT_EPOCH` in `scripts/review_autofix_step_changes_lost_redispatch.sh` are kept for caller compatibility and ignored. `tests/test_retrigger_default_branch_dispatch.py::test_budget_ignores_a_backdated_commit` covers the finding.
