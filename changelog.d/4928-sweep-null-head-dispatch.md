<!-- changelog: security -->
- **The review autofix sweep now sees a dispatched review run named for its PR even when GitHub reports no head branch for it, so it no longer dispatches that PR a second time.** This closes security finding #4928 (`sweep-discards-null-head-dispatch`, medium, STRIDE: Denial of Service).

`review_autofix_sweep.yml` skips a PR that already has a queued, running, or pending review run. It finds a run started from the default branch by its name, `Internal: AI Review & Autofix [pr:<N>]`, and counts it under the key `pr:<N>`. GitHub can report `head_branch` as null on a `workflow_dispatch` run, and the sweep's active-run snapshot dropped every run without a head branch before it read the name. The PR then looked idle, so the next tick dispatched it again and the new run replaced the pending review in the `review_autofix` concurrency group. The snapshot now keeps such a run under its `pr:<N>` key. A run that has neither a head branch nor a valid PR name is still dropped.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls per sweep tick | 0 (the fix reads the run listing the sweep already fetches) |
| Runs newly counted | `workflow_dispatch` runs titled exactly `Internal: AI Review & Autofix [pr:<N>]` with a null, missing, or empty `head_branch` |
| Queued-run cutoff | unchanged, `SWEEP_STALE_QUEUED_MINUTES` (default 120) |

What this means for operators: a pending review run for a PR is no longer replaced by a duplicate sweep dispatch when GitHub omits the run's head branch. A wedged queued run is still logged as `AUTOFIX_SWEEP_STALE_QUEUED` under its `pr:<N>` key and stops suppressing dispatch after the usual cutoff.
