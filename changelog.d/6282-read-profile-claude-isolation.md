<!-- changelog: fixed -->
- **Read-profile Claude calls no longer see runner credentials.** They execute in a read-only, network-isolated container with sanitized repository snapshots; the host relay retains the OAuth token. Isolation failures use the caller's existing fallback instead of running Claude on the host.

### For contributors

`scripts/ai_engine.sh` isolates all read-profile calls, including `AI_ENGINE_READ_ONLY=true`. `AI_ENGINE_READ_EXTRA_DIRS` permits additional sanitized worktree snapshots; snapshot and runtime limits have defaults documented in the README. `CLAUDE_READ_ISOLATION` reports snapshot status without file contents or credentials.
