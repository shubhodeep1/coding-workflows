<!-- changelog: changed -->
- **The merge train now queues a PR only behind older PRs it really conflicts with, bounds how long one head can block, and lets priority fixes go first.**

Before, any younger `ai/issue-*` PR that touched a path an older open `ai/issue-*` PR also touched was labelled `ai:merge-queued`, even when git could merge the two cleanly. One long-running head could then hold the whole queue: 18 PRs on `main` waited behind #6135, and the release-gate fix #6464 was queued 18th. `scripts/review_merge_train.sh` now applies three default-on checks to each overlapping older PR, in both `gate` and `release`:

- `MERGE_TRAIN_CONFLICT_CHECK_ENABLED` (default `true`): the older PR blocks only when `git merge-tree --write-tree` of the two heads conflicts. Any git error keeps the old path-overlap rule.
- `MERGE_TRAIN_HEAD_MAX_AGE_HOURS` (default `24`, `0` disables it): an older PR that is not itself queued and has been under review longer than the cap stops blocking. Incomplete or unverifiable label history keeps the blocker.
- `MERGE_TRAIN_PRIORITY_LABELS` (default `ai:workflow-heal,ai:security`; `none` disables it): a PR whose verified `ai/issue-<M>` closing issue carries one of these labels does not wait behind non-priority PRs. Two priority PRs keep lowest-number-first.

The head-age and priority checks share one aliased GraphQL read per run. The conflict check makes no API calls. Each overlapping PR is logged as a `MERGE_TRAIN_GATE … conflict=… priority=… stale=… action=skip|block` line. The three variables are wired into `review_autofix.yml`, `orchestrate_poll.yml` and `cancel_on_pr_close.yml`, so gate and release always read the same settings.

What this means for operators: unrelated PRs that share a file no longer wait in line, and a stuck head blocks for at most a day. To restore the old behaviour, set `MERGE_TRAIN_CONFLICT_CHECK_ENABLED=false`, `MERGE_TRAIN_HEAD_MAX_AGE_HOURS=0` and `MERGE_TRAIN_PRIORITY_LABELS=none`.
