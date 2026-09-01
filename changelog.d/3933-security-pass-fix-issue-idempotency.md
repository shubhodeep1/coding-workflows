<!-- changelog: fixed -->
- **Project security-pass fix issue creation is now idempotent across lost state checkpoints.** Before creating a cycle issue, the poller reuses an open orchestrator-managed issue carrying the same tracking-issue and cycle Local ID markers, then republishes that issue number in authoritative state instead of creating a duplicate.
