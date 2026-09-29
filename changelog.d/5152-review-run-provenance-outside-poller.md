<!-- changelog: security -->
- **Four more review-run guards now check where a PR-named run came from.** A run named `Internal: AI Review & Autofix [pr:<N>]` or `AI Review [pr:<N>]` counts only when it is a `workflow_dispatch` run of the wrapper that sets that name, dispatched from the default branch.

GitHub evaluates a run's name from the workflow file at the dispatched ref, so anyone who can push a branch could dispatch a copy of `internal-review.yml` from it and name the run for another PR. Before this change, such a run could suppress the post-commit review retrigger, use up the one editor-changes-lost retry, keep a merge-queued PR from being released, make the review sweep skip a PR, or keep a Claude check-in waiting instead of reporting the PR stuck. The rule issue #5094 gave the orchestrator poller now also covers `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, `_mt_inflight_review_branches` in `scripts/review_merge_train.sh`, the sweep's `dispatch_pr` in `.github/workflows/review_autofix_sweep.yml`, and `_active_run_count` in `.claude/scripts/check_in_status.py`. A run with a null `head_branch` still counts, because GitHub reports one on genuine dispatch runs (issue #4928).

| The numbers that matter | Value |
| --- | --- |
| Guards hardened | 4 |
| New API calls per run | 0 when the event payload names the default branch; otherwise 1 `GET repos/<repo>` per process (sweep: per run) |
| Log line when the default branch is unknown | `REVIEW_RUN_PROVENANCE ... outcome=default_branch_unresolved pr_named_matching=disabled` |

What this means for operators: nothing changes for runs the automation dispatches itself. A run dispatched from any other branch can no longer hold back a review, a retry, a merge-train release, or a stuck-PR hand-off. If a `REVIEW_RUN_PROVENANCE` warning appears, the job could not read the default branch, and PR-named matching was off for that run.

### For contributors

The default branch is never guessed. `_autofix_review_default_branch` reads `.repository.default_branch` from `$GITHUB_EVENT_PATH` and falls back to one REST read. The sweep uses `EVENT_DEFAULT_BRANCH` the same way, and `check_in_status.py` passes the PR object's `base.repo.default_branch` to `_active_run_count` through a new optional `default_branch` argument. The sweep and `check_in_status.py` still match only the internal name, because they list only `internal-review.yml` runs.
