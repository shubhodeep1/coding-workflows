<!-- changelog: changed -->
- **The review autofix sweep no longer dispatches reviews for merge-train queued PRs.** `Internal: AI Review Autofix Sweep` skips a PR labelled `ai:merge-queued` while `MERGE_TRAIN_ENABLED` is on.

Every 30 minutes the sweep dispatched a review for every open non-draft PR, including the ones `scripts/review_merge_train.sh` holds behind older overlapping PRs. Each of those runs only re-ran the `Merge-train gate` and soft-exited, spending runner setup and one file-list call per older PR to confirm the PR was still queued, while the train's `release` tick in `orchestrate_poll.yml` already re-evaluates every queued PR and re-dispatches its review once the blockers are gone. The sweep now logs `AUTOFIX_SWEEP_SKIP pr=#<N> reason=merge_train_queued` for those PRs and reports them as `skipped_merge_queued=` in `AUTOFIX_SWEEP_END`. A disabled train and a label removed by hand (the one-shot bypass) both dispatch again, so no PR is left unreviewed.

| The numbers that matter | Value |
| --- | --- |
| Sweep tick that prompted the change | run 37895976433, 2026-10-09 06:54 UTC |
| Reviews it dispatched | 31, including every PR in a 25-deep train |
| Repository variable honoured | `MERGE_TRAIN_ENABLED` (`true`/`1`/`yes`/`on`, case-insensitive) |

What this means for operators: a queued PR's review is dispatched by the train's release tick, not by the sweep, so a queue no longer costs a review run per PR every half hour. Removing `ai:merge-queued` by hand still brings the PR back into the next sweep.

### For contributors

`tests/test_review_autofix_sweep_merge_queued_skip.py` runs the shipped sweep step against a fake `gh` for the skip, the disabled-train and bypass paths, and the PR snapshot shape `scripts/ci_cancelled_rerun.py` reads.
