<!-- changelog: fixed -->
- **`HEAL_SYNCED_SECURITY_LINE_BUCKET` now has a maximum of 1,000,000, so an oversized bucket no longer breaks every synced-file security report.** Before, a bucket above about 2,000,000 made every finding group fail to load in `scripts/workflow_failure_heal.py` ("invalid group bucket"), so the `workflow-failure-heal-intake.yml` run logged `issue_compose_failed` and opened no issue.

The bucket is clamped to 1,000,000, the largest line a finding may name, the same way values below `1` already count as `1`. Buckets up to that value keep their fingerprints. The consumer-side warning in `scripts/security_audit_upstream_report.sh` for a failed dispatch also no longer says "the next audit retries": incremental audits report a synced file again only after a sync changes it, so the warning now says the report is not retried.

What this means for operators: a bucket set too high now behaves like the maximum instead of silently stopping synced-file security issues, and a failed report is described accurately.
