<!-- changelog: fixed -->
- **Merge-train passes no longer stop on a run listing that keeps shifting.** A listing whose re-reads together cover GitHub's reported run count is now complete.

`scripts/review_merge_train.sh release` reads every queued and running workflow run before it releases a queued PR. While runs churn, GitHub's `total_count` and its listing can disagree on several reads in a row; at 09:56 UTC on 2026-10-10 that made one pass leave all 45 queued PRs unexamined. The listing is now accepted when the runs read across its re-reads number at least the latest count, logged as `MERGE_TRAIN_RUNS_LISTING outcome=accepted reason=union_covers_total`.

| The numbers that matter | Value |
| --- | --- |
| Queued PRs skipped by the 09:56 pass | 45 |
| Pages the new check covers | first page of each status |
| Re-read budget (`MERGE_TRAIN_RUNS_LISTING_SHIFT_RETRIES`) | unchanged, default `2` |

What this means for operators: fewer merge-train ticks end with `MERGE_TRAIN_RELEASE_RUNS_INCOMPLETE` while the pipeline is busy. A run that changed status between reads can keep one PR queued for one more tick.
