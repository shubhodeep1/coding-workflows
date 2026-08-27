<!-- changelog: fixed -->
- **The review autofix sweep no longer deadlocks behind a workflow run GitHub has wedged.** `review_autofix_sweep.yml` now stops treating a `queued` run as active once it passes `SWEEP_STALE_QUEUED_MINUTES` (default 120), so a PR whose review run never started gets picked up on the next 30-minute tick instead of stalling indefinitely.

The sweep skips any PR that already has a `queued` or `in_progress` review run on its head ref, which stops a tick from stomping a synchronize-fired run mid-edit. That check had no time cutoff, and GitHub can leave a run wedged in `queued` with zero jobs while rejecting both `cancel` (409 `Cannot cancel a workflow run that has not been queued yet`) and `rerun` (403 `This workflow is already running`). Nothing could clear such a run, and because the sweep is the only recovery path for the PR behind it, the guard deadlocked the mechanism it protects. On `shubhodeep1/coding-workflows#3841` this stalled the review-blocked judge for over 11 hours while every tick logged `AUTOFIX_SWEEP_SKIP pr=#3841 reason=active_run`. `in_progress` runs are still never discounted, since the codex-agent job legitimately runs well over an hour.

| The numbers that matter | Value |
| --- | --- |
| New repo var | `SWEEP_STALE_QUEUED_MINUTES` (default `120`) |
| Longest observed legitimate concurrency wait | ~94 minutes |
| Sweep cadence | every 30 minutes |
| Time PR #3841 stalled behind the wedged run | 11+ hours |
| New tests | 11 in `tests/test_review_autofix_sweep_stale_queued.py` |

What this means for operators: a review run that GitHub fails to start no longer strands its pull request. The sweep reports each discounted run as an `AUTOFIX_SWEEP_STALE_QUEUED` warning naming the workflow, head ref, and run id, so the wedged run is visible in the tick log rather than silently ignored. Setting `SWEEP_STALE_QUEUED_MINUTES=0` restores the previous always-suppress behaviour.

### For contributors

The cutoff is computed in bash and passed to jq as `--argjson cutoff`, so the reduce stays a pure function of its input and never calls `now`. A run whose `created_at` is missing or unparseable still counts as active, which fails toward the old behaviour rather than toward a duplicate dispatch. `tests/test_review_autofix_sweep_stale_queued.py` extracts the jq program from the workflow file itself and executes it, so an edit that drops the cutoff cannot pass against a stale copy of the reduce.
