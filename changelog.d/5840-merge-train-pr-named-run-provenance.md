<!-- changelog: security -->
- **The merge train now trusts a review run named for a pull request only when the run came from the default branch and from the review wrapper that sets that name.** This fixes security finding #5840 (`merge-train-release-spoofed-by-branch-run`).

GitHub evaluates a `workflow_dispatch` run's name from the workflow file at the dispatched ref. Anyone who can push a branch could dispatch a run titled `AI Review [pr:<N>]` or `Internal: AI Review & Autofix [pr:<N>]` from that branch and keep it active. The `release` step of `scripts/review_merge_train.sh` took the title at face value and left PR N labelled `ai:merge-queued` (`MERGE_TRAIN_RELEASE_ACTIVE … action=leave_queued`) for as long as that run lived. A PR-named run now holds a queued PR only when its event is `workflow_dispatch`, its head branch is the repository's default branch, and its path is `.github/workflows/ai-review.yml` (for `AI Review …`) or `.github/workflows/internal-review.yml` (for `Internal: AI Review & Autofix …`). This is the rule #5094 applied to the orchestrator poller.

| The numbers that matter | Value |
| --- | --- |
| New API calls per `release` run | 0 (the default branch is read from `base.repo.default_branch` in the open-PR listing `release` already makes) |
| Run keys that hold a queued PR | head-branch match (unchanged), or a PR-named run that passes all three checks above |

What this means for operators: legitimate releases are unaffected, because every PR-named review dispatch in this library runs from the default branch through the wrappers (issues #4618, #4701, #4898). A review run on the PR's own head branch still holds it back. If the default branch cannot be read from the open-PR listing, `release` logs `MERGE_TRAIN_PR_NAMED_PROVENANCE repo=<repo> outcome=default_branch_unresolved pr_named_matching=disabled` and ignores PR-named runs for that run, so at worst a queued PR gets a second review dispatch.

### For contributors

`_mt_inflight_review_branches` keeps its name and output shape (one key per line, head branches and `pr:<N>`) and takes the default branch as an optional first argument; with none it prints no `pr:<N>` key. Its response is now filtered by `jq --arg` because `gh api --jq` takes no arguments. `_mt_list_open_prs` adds a `default_branch` field to each JSON line. The other PR-named matchers (`_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, `review_autofix_sweep.yml`, `.claude/scripts/check_in_status.py`) are outside this fix.
