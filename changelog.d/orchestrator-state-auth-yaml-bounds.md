<!-- changelog: security -->
- **Orchestrator state and deterministic contract merges now enforce their trust and resource boundaries.** Tracking state is accepted only from trusted bot or repository-associated authors, unauthenticated markers cannot veto safe reconstruction, non-empty integration branches must match the current tracking issue, and branch mutations repeat that binding check before access.

Contract entrypoint union inputs are capped at 1 MiB per side and accept at most 4,096 scalar strings of 4,096 characters each. Set-based comparisons and a fixed 10-second helper timeout prevent recursive YAML aliases or oversized conflicts from stalling the poller; rejected inputs continue through the existing resolver path on the correctly bound branch.
