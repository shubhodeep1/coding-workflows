<!-- agents: section="Stable log prefixes (contractual)" -->
- `ORCH_TRACKING_ISSUE_LIST_TRUNCATED` (`.github/workflows/orchestrate_poll.yml`, step `find_tracking`: `::warning::ORCH_TRACKING_ISSUE_LIST_TRUNCATED limit=<int> count=<int>`, emitted when the open `ai:orchestrator-tracking` listing returned at least `ORCHESTRATE_TRACKING_ISSUE_LIST_LIMIT` (default 100, 1-1000) issues, so older projects may be missing from `tracking_issues.json` that tick; integers only).
LOG_PREFIX.name=ORCH_TRACKING_ISSUE_LIST_TRUNCATED
