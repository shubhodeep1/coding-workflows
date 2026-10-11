<!-- agents: section="Workflow architecture" -->
Workflow failure heal issue creation is gated by
`HEAL_INTAKE_SCOPE_GUARD_ENABLED` (default `false`, #6871). While it is off,
the intake composes the heal issue, logs
`WORKFLOW_HEAL skip reason=scope_guard_disabled` and files no issue or outcome
comment (the `ai:workflow-heal` / `ai:workflow-heal-escalated` labels in this
repository are still ensured, as for every report). When it is `true`, the composed body's
`ai:workflow-heal-scope:v1` marker must pass `heal-scope verify` before
`gh issue create`; a missing or invalid marker logs
`reason=scope_marker_unverified`, sends a CRITICAL alert and fails the run.
