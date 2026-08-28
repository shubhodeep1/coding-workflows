<!-- changelog: changed -->
- **Review/autofix read-side model calls now run through OpenCode.** Both reviewer passes, cache probes, and consensus summarisation use isolated read-only OpenCode configurations with the existing reasoning, retry, failback, heartbeat, and ledger contracts. Codex remains installed and continues to power editor, judge, consolidator, and conflict-resolution stages.
