<!-- changelog: security -->
- **The orchestrator poller, the merge train, and the forward-merge fallback now start review runs from the default branch only, never from a pull request's unmerged head branch.** This closes the rest of security finding #4618 (`review-dispatches-unmerged-workflow`), which #4618 fixed for the review autofix sweep.

Three more places dispatched the review workflow with `--ref <PR head branch>`, so each ran that branch's own copy of the workflow file with `secrets: inherit` and write permissions: `_dispatch_review_for_conflicts` in `scripts/orchestrate_poll_process.sh` (conflicts, stall recovery, review-blocked unstick), `_mt_dispatch_review` in `scripts/review_merge_train.sh`, and the fallback-PR step of `forward-merge-stable-to-main.yml`. All three now dispatch without `--ref` and pass only a PR number they have checked is a positive integer. `review_autofix.yml` still checks out the PR head from the PR's metadata, so reviews behave as before.

A run started from the default branch shows the default branch as its head, so the poller now finds review runs by name as well as by branch. The consumer `ai-review.yml` names dispatched runs `AI Review [pr:<N>]`, next to #4618's `Internal: AI Review & Autofix [pr:<N>]`. Six places use the names. Three need an extra API call: the active-run guard, the stall-recovery failed-run redispatch, and the last push-guard fallback. The other three read data the poller already has: the stall judge's run list, the empty-commit push guards, and the merge-train release.

| The numbers that matter | Value |
| --- | --- |
| Review dispatches that run an unmerged branch's workflow file | 0 (was: 3 sites, plus the sweep before #4618) |
| New GitHub API calls per lookup | at most 1 `gh run list --event workflow_dispatch --limit 100`, only when the branch lookups found nothing |
| New calls in the stall judge, push-guard cached scans, and merge-train release | 0 |

What this means for operators: conflict, merge-train, and forward-merge review runs now show the default branch in the Actions list, with the PR in the run name. Pull request and push runs keep their usual names. In consumer repos, dispatched runs are found by name after the next workflow sync brings in the new `ai-review.yml`. Until then a poller on the new scripts can dispatch a duplicate review for a PR whose run it cannot see; the per-cycle dispatch tracker and the per-PR concurrency group bound this.

### For contributors

`_pr_named_review_dispatch_runs <pr>` in `scripts/orchestrate_poll_process.sh` is the shared lookup. It prints a JSON array, newest first, and fails open to `[]`. `_direct_inflight_review_run_on_branch` takes an optional second argument, the PR number. `MERGE_TRAIN_DISPATCHED` keeps its `ref=` field, which now reads `ref=default`, and adds `head=`. `review_autofix.yml`, the last dispatch candidate after both wrappers, has no PR run name.
