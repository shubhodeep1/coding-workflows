<!-- changelog: fixed -->
- **Orchestrator state can no longer be forged or replayed to expand conflict-resolver write scope.** State comments are authenticated to the active workflow producer and project context, signed generations prevent stale-state replay, unsigned legacy state is migrated only by the poller, and resolver fingerprints are structurally sanitized before they can add files to the privileged allowlist.

What this means for operators: associated users and unrelated bots cannot inject state-derived resolver paths; missing or invalid authentication disables only fingerprint-driven scope expansion while ordinary git conflict resolution remains available.
