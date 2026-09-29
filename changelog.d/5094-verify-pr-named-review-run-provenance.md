<!-- changelog: security -->
- **The orchestrator poller now trusts a review run named for a pull request only when the run came from the default branch and from the review wrapper that sets that name.** This fixes security finding #5094 (`poller-trusts-unverified-review-run-name`).

GitHub evaluates a `workflow_dispatch` run's name from the workflow file at the dispatched ref. Anyone who can push a branch could therefore dispatch a run titled `Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]` for another pull request. The poller took that title at face value. It skipped the conflict dispatch, held back the stall-recovery empty-commit push, or treated a forged failure as the pull request's own review result. In `scripts/orchestrate_poll_process.sh` a PR-named run now counts only when its event is `workflow_dispatch`, its head branch is the repository's default branch, and its workflow path is `.github/workflows/internal-review.yml` (internal name) or `.github/workflows/ai-review.yml` (consumer name). The same check applies in the active-run guard, the stall-recovery failed-run redispatch, both empty-commit push guards, and the stall judge's run list.

| The numbers that matter | Value |
| --- | --- |
| PR-named match sites in the poller that check workflow path and default-branch head | 4 of 4 (was 0) |
| API calls per PR-named lookup | still 1: REST `actions/runs?event=workflow_dispatch&branch=<default>&per_page=100` replaces `gh run list --event workflow_dispatch --limit 100` |
| New calls per poller run | at most 1 `GET repos/<repo>` for the default branch |

What this means for operators: legitimate review dispatches are unaffected, because they all run from the default branch through the wrappers. A PR-named run started on a pull request's own branch is still seen through the head-branch lookups. If the default branch cannot be read, the poller logs `PR_NAMED_REVIEW_PROVENANCE outcome=default_branch_unresolved` and ignores PR-named runs for that poll, as it did before PR-named runs existed.

### For contributors

`_pr_named_review_dispatch_runs <pr>` keeps its name, arguments, and output shape (`[{databaseId, event, status, conclusion, displayTitle, createdAt, startedAt}]`). The new `_pr_named_review_default_branch` caches the default branch in `_PR_NAMED_REVIEW_DEFAULT_BRANCH_CACHE` and is primed once in the main shell. It does not reuse `DEFAULT_BRANCH`, which the poller sets with a `main` fallback. The other PR-named matchers (`_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, `scripts/review_merge_train.sh`, `review_autofix_sweep.yml`, `.claude/scripts/check_in_status.py`) are outside this fix.
