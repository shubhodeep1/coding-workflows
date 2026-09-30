<!-- changelog: security -->
- **The review sweep and the review retrigger steps now trust a review run named for a pull request only when it came from the default branch and from the wrapper that sets that name.** This fixes security finding #5522 (`sweep-accepts-spoofed-pr-review-run`).

GitHub evaluates a `workflow_dispatch` run's name from the workflow file at the dispatched ref. Anyone who can push a branch could therefore dispatch an altered `internal-review.yml` from that branch, titled `Internal: AI Review & Autofix [pr:<N>]` for another pull request. `review_autofix_sweep.yml` then skipped that pull request's scheduled review while the forged run stayed active, even when GitHub reported the run's head branch as null. The sweep now gives a run a `pr:<N>` key only when its event is `workflow_dispatch`, its head branch is the repository's default branch, and its workflow path is `.github/workflows/internal-review.yml`. Any other run is keyed by its own head branch. The same check now applies to `_autofix_pr_named_review_runs` in `scripts/gh_helpers.sh`, which `review_autofix.yml`'s post-commit retrigger and changes-lost re-dispatch use to find an in-flight peer and to count the per-head retry budget. Consumer runs are matched with `AI Review [pr:<N>]` and `.github/workflows/ai-review.yml`.

| The numbers that matter | Value |
| --- | --- |
| PR-named matchers that now check the default-branch head and the wrapper path | 2 (the sweep snapshot and `_autofix_pr_named_review_runs`) |
| New API calls per sweep tick | 0 (the default branch comes from the open-PR listing) |
| New API calls per retrigger step | at most 1 `GET repos/<repo>`, only when the PR-named lookup runs |

What this means for operators: legitimate review dispatches are unaffected, because they all run from the default branch through the wrappers. A PR-named run whose head branch GitHub reports as null no longer holds back the sweep (this reverses the #4928 allowance). At worst the next tick dispatches that pull request again, and the new run joins the same `review_autofix` concurrency group. When the sweep cannot read the default branch, it logs `AUTOFIX_SWEEP_PROVENANCE outcome=default_branch_unresolved` and ignores run names for that tick. When the retrigger steps cannot read it, they log `AUTOFIX_PR_NAMED_PROVENANCE outcome=default_branch_unresolved`. The peer check then dispatches anyway, and the changes-lost retry budget fails closed.

### For contributors

`_autofix_pr_named_review_runs` keeps its name, arguments, and output shape (`[{id, status, conclusion, created_at, path}]`). The new `_autofix_pr_named_review_default_branch` caches the default branch in `_AUTOFIX_PR_NAMED_REVIEW_DEFAULT_BRANCH_CACHE`, and both callers prime that cache before their `$(...)`. The merge train (#5524) and `.claude/scripts/check_in_status.py` (#5521) are fixed in their own projects.
