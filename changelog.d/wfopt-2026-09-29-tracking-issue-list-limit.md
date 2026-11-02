<!-- changelog: fixed -->
- **The orchestrator poller now polls up to 100 open projects per tick, not 20, and warns when it hits the limit.**

The `find_tracking` step in `.github/workflows/orchestrate_poll.yml` listed open `ai:orchestrator-tracking` issues with a fixed `--limit 20`, so with more than 20 open projects the oldest were left out of every tick and their waves stopped dispatching. The step now lists up to `ORCHESTRATE_TRACKING_ISSUE_LIST_LIMIT` (default 100, integer 1-1000) issues per tick. An invalid value falls back to 100 with a warning, and a listing that reaches the limit emits `ORCH_TRACKING_ISSUE_LIST_TRUNCATED limit=<n> count=<n>`. Refs #8663.
