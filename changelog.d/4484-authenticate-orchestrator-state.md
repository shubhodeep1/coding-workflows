<!-- changelog: fixed -->
- **Orchestrator state comments now require workflow authentication.** Tracking-state and standalone stall-state reads reject unsigned or tampered comments instead of allowing issue commenters to forge security-pass or recovery state.

The initial-state and poller writers sign existing V1/V2 and standalone frames with the workflow credential, scoped to repository, issue, and record type. The poller verifies the signature and comment author before using a state record or updating its comment. Unverifiable historical state pauses reconstruction instead of inheriting a claimed security pass.
