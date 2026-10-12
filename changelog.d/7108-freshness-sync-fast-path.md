<!-- changelog: fixed -->
- **An approved PR updated from `main` now merges after CI instead of starting a new review cycle.** At most 2 such updates happen per PR.

The merge-base freshness gate updates a PR from its base when the base changed files the PR touches. Each update used to restart the full review cycle, which took about 20-25 minutes, and on busy days `main` moved again before it finished: #6654 was updated four times in two hours. The gate now records the head it approved in a PR comment. The next review run checks that the new head is only GitHub's merge of that head with the base, skips the reviewers, waits for CI and merges. After `MERGE_BASE_FRESHNESS_MAX_SYNCS` updates, a green CI run merges without another update.

| The numbers that matter | Value |
| --- | --- |
| Updates per PR before merging on green CI | 2 (`MERGE_BASE_FRESHNESS_MAX_SYNCS`) |
| Updates #6654 needed before this change | 4 in two hours |
| Kill switch for the fast path | `MERGE_BASE_SYNC_FAST_PATH_ENABLED=false` |
| Extra API reads per update | 2 to count, 2 to verify (only when a marker exists) |

What this means for operators: queued PRs that touch busy files (`ci.yml`, `README.md`, `review_autofix.yml`) stop looping between "merge" decisions and branch updates. The `force-review` label still forces a full review of an updated head.
