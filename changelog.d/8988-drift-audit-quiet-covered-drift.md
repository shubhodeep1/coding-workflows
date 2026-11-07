<!-- changelog: fixed -->
- **The consumer drift audit no longer warns about drift that an open sync pull request already fixes.** It warns again if that pull request stays open for 3 days.

The daily `audit_consumer_drift.yml` run sent a Telegram WARNING whenever a consumer drifted from the `stable` release, even when the consumer had an open `auto/update-workflows-*` or `ai-sync/stable` pull request that restores every wrapper. That drift is now reported at `DEBUG` with a "nothing to do" message naming each pull request, which the default `ALERT_MSG_LEVEL=WARNING` hides. A sync pull request open for 3 days or longer, or one whose open date cannot be read, counts as stuck and raises a WARNING that names it. Fetch errors, failed repairs, new or refreshed restore pull requests, and drift with no sync pull request still warn as before.

| The numbers that matter | Value |
| --- | --- |
| Stuck after | 3 days (`SYNC_PR_STALE_AFTER_SECONDS`) |
| Extra GitHub API calls | 0 (open date comes from the existing pull request listing) |
| New `DRIFT_REPAIR` field | `stale=true\|false` on `reason=sync_pull_request_open` |

What this means for operators: a drift WARNING now always needs action, either a stuck sync pull request to merge or close, or a problem the audit could not fix itself.

### For contributors

The summary JSON's `repairs` entries gain `pr_created_at` and `pr_stale`. The report step treats a missing `pr_stale` (older summaries) as stale.
