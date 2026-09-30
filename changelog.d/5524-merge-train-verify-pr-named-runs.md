<!-- changelog: security -->
- **The merge train now counts a review run named for a pull request as active only when the run came from the default branch and from the review wrapper that sets that name.** This fixes security finding #5524 (`merge-train-accepts-spoofed-pr-review-run`).

GitHub evaluates a `workflow_dispatch` run's name from the workflow file at the dispatched ref. Anyone who can push a branch could therefore dispatch a run titled `AI Review [pr:<N>]` or `Internal: AI Review & Autofix [pr:<N>]` for a queued pull request. `scripts/review_merge_train.sh release` took that title at face value and left the pull request labelled `ai:merge-queued` (`MERGE_TRAIN_RELEASE_ACTIVE … action=leave_queued`), so its review never ran. A PR-named run now holds a pull request only when its event is `workflow_dispatch`, its head branch is the repository's default branch, and its workflow path is `.github/workflows/internal-review.yml` (internal name) or `.github/workflows/ai-review.yml` (consumer name). This is the same check #5094 added to the orchestrator poller.

| The numbers that matter | Value |
| --- | --- |
| New API calls per release | 0: the default branch comes from `base.repo.default_branch` in the open-PR listing the release already makes |
| Approved (path, run name) pairs | 2 |

What this means for operators: legitimate review dispatches are unaffected, because they all run from the default branch through the wrappers, and a run on the pull request's own branch still holds it through the head-branch key. If the open-PR listing carries no default branch, the release logs `::warning::MERGE_TRAIN_PR_NAMED_PROVENANCE … outcome=default_branch_unresolved pr_named_matching=disabled` and ignores PR-named runs for that tick, so it may dispatch a review beside one that is already running.

### For contributors

`_mt_inflight_review_branches` keeps its name and output (one key per line). It now takes an optional default-branch argument and filters the run rows locally with `jq --arg`, because `gh api --jq` cannot take arguments. Each open-PR row from `_mt_list_open_prs` gains a `default_branch` field. The other PR-named matchers (`_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, `review_autofix_sweep.yml`, and `.claude/scripts/check_in_status.py`) are outside this fix.
