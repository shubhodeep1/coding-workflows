<!-- changelog: fixed -->
- **The merge train's real-conflict check now works in the poller and PR-close runs.** Queued PRs that merge cleanly with an older overlapping PR are released instead of waiting behind it.

`scripts/review_merge_train.sh` decides whether an older PR that edits the same files really blocks a queued one by running `git merge-tree` on the two heads. The `release` runs in `orchestrate_poll.yml` and `cancel_on_pr_close.yml` happen outside any git checkout, so that probe always failed, logged `conflict=unknown`, and kept the blocker: one older PR stuck in review could hold every younger overlapping PR. The probe now creates a private bare repository under `RUNNER_TEMP` when there is no checkout, fetches the two heads by SHA at depth 200, and deletes the repository on exit. `GH_TOKEN` reaches git only through `GIT_CONFIG_*` environment entries. Any failure still keeps the blocker.

| The numbers that matter | Value |
| --- | --- |
| Queued PRs blocked with `conflict=unknown` on the 2026-10-09 21:58 poll tick | 45 of 45 |
| Depth-200 fetch of coding-workflows into an empty repository | about 5 s, 12 MB |
| New log line | `MERGE_TRAIN_PROBE_REPO outcome=created origin=<url>` or `outcome=unavailable reason=<r>` |

What this means for operators: the merge queue drains on the next poll ticks without workflow changes in consumer repos; PRs that truly conflict with an older one still wait for it.
