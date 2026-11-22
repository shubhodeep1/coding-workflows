<!-- changelog: changed -->
- **The merge lease now logs metrics for every gate and tick decision.** A new `MERGE_LEASE_METRIC` line follows each `MERGE_LEASE` line. It gives the event, the base, the PR, the holder, how long the holder has held the lease (`hold_s`) and the number of waiting PRs (`waiters`), so later phases of the merge-lease parallel-merges project can measure wait times, hold times and grant, release and wait counts per base. A `released` line also ends with `merged=true|false`, read from the holder-state call the tick already makes, so the share of holds that ended without a merge can be measured.

This is observability only. No gate, grant or release decision, return code, lease record, label or GitHub API call changes, and the existing `MERGE_LEASE` line is unchanged. A wait time is the time from a PR's first `event=wait` `ts` to its `event=acquired` or `event=granted` `ts`. `MERGE_LEASE_METRICS_ENABLED` (default `true`, set in the helper) turns the lines off. If emitting a metric fails, the lease still works: `_merge_lease_metric` always returns 0, and the merge-train tick skips metrics when the staged `pr_checks_lib.sh` predates the helper.

What this means for consumer repos: after the next `@stable` sync, review and merge-train logs gain `MERGE_LEASE_METRIC` lines. Nothing needs to be done.

Refs #9781
