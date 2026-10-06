<!-- changelog: fixed -->
- **Conflict resolution no longer runs an OpenCode writer on the credentialed host.** Both resolver engines use isolated snapshots and validated transfer; unsupported conflict paths are refused before any model runs and count toward integration-sync resolver escalation.
