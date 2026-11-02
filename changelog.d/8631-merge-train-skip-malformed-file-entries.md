<!-- changelog: fixed -->
- **The merge train now skips malformed entries in a PR's file list instead of losing the whole list.**

`scripts/review_merge_train.sh` read each PR's files from `pulls/<n>/files`. One entry that was not an object made the whole lookup fail, and the gate then let the PR through without checking overlaps. An entry whose `filename` was null or empty added a literal `null` path. Now such entries add no path and the rest of the list is still used. Renamed files still count under both their old and new paths, and there is still one paginated call per PR per run.
