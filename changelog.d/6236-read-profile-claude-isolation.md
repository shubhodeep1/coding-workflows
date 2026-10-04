<!-- changelog: fixed -->
- **Read-profile Claude calls no longer expose pool tokens to the model process.** The CLI runs in a network-isolated container with a placeholder token while a host relay holds the real credential. If isolation cannot start, the caller uses its existing codex/OpenCode fallback instead of running Claude on the host.
