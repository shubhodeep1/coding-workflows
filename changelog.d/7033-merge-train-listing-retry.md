<!-- changelog: changed -->
- **The merge train now re-reads a workflow-run listing that changed while it was paging, instead of skipping the release pass.**

`scripts/review_merge_train.sh` lists the review runs in flight before it releases queued PRs. When a run started or finished between two page reads, a page came back short and the train aborted the whole release pass for that cycle. It now re-reads the listing from page 1, by default twice with a 2-second pause, and fails closed as before only after those retries are used up. Each retry logs `MERGE_TRAIN_RUNS_LISTING outcome=retry reason=listing_shifted`.

| The numbers that matter | Value |
| --- | --- |
| `MERGE_TRAIN_RUNS_LISTING_SHIFT_RETRIES` | default 2, range 0-5 (0 = old behaviour) |
| `MERGE_TRAIN_RUNS_LISTING_SHIFT_RETRY_SLEEP` | default 2 seconds, range 0-99 |

What this means for operators: busy cycles release ready PRs instead of losing the cycle to a listing that moved underneath the train.
