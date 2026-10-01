<!-- changelog: security -->
- **The review workflow's retrigger probes now trust a review run named for a pull request only when it was dispatched on the default branch from the wrapper that sets that name.** This fixes security finding #5838 (`autofix-retrigger-accepts-untrusted-review-runs`), the workflow-side twin of the poller fix in #5094.

GitHub evaluates a `workflow_dispatch` run's name from the workflow file at the dispatched ref. Anyone who can push a branch could therefore dispatch a modified review wrapper titled `Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]` for another pull request. `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh` took that title at face value. A forged in-flight run made the `Re-trigger review via workflow_dispatch` step skip the pull request's next review, and a forged completed run used up its one `Re-dispatch review on editor-changes-lost` retry. A PR-named run now counts only when its event is `workflow_dispatch`, its head branch is the repository's default branch, and its path is exactly `.github/workflows/internal-review.yml` (internal name) or `.github/workflows/ai-review.yml` (consumer name).

| The numbers that matter | Value |
| --- | --- |
| PR-named probes in `review_autofix.yml` that check head branch and exact wrapper path | 2 of 2 (was 0) |
| New GitHub API calls in Actions | 0; the default branch comes from the run's event payload |
| Fallback when the payload lacks it or names another repository | 1 `GET repos/<repo>` per lookup |

What this means for operators: legitimate review dispatches are unaffected, because both retrigger steps dispatch from the default branch through the wrappers. If the default branch cannot be resolved, the step logs `AUTOFIX_PR_NAMED_REVIEW_PROVENANCE … outcome=default_branch_unresolved` and the PR-named lookup counts as failed. The in-flight check then lets the dispatch go ahead, as it always did on an API error. The editor-changes-lost retry is skipped (`reason=pr_named_api_error`). A wrapper renamed to `ai-review.yaml` is no longer recognised, matching the poller since #5094.

### For contributors

`_autofix_pr_named_review_runs <pr> [status]` keeps its arguments, output shape (`[{id, status, conclusion, created_at, path}]`), and return codes. The new `_autofix_pr_named_review_default_branch` reads `.repository.default_branch` from `$GITHUB_EVENT_PATH` only when `.repository.full_name` matches `GITHUB_REPOSITORY`, and otherwise reads `repos/<repo>` once. It rejects an empty answer or one holding whitespace or control characters. `AUTOFIX_PEER_CHECK` and `AUTOFIX_CHANGES_LOST_BUDGET` are unchanged. A `workflow_dispatch` run of another branch is not PR-named, so the budget probe treats it as `unnamed_dispatch_run` and skips its retry. The merge train, the review sweep, and `.claude/scripts/check_in_status.py` keep their own PR-named matchers, which this fix does not cover.
