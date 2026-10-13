<!-- agents: section="Workflow architecture" -->
Heal-scope recovery (#7191, the operator step for #7082) is gated by
`HEAL_SCOPE_RECOVERY_ENABLED` (default `false`). With it, and
`HEAL_INTAKE_SCOPE_GUARD_ENABLED`, set to `true`, a report whose OIDC
identity verified (`report_auth=verified`) and whose fingerprint matches an
open heal issue with no `ai:workflow-heal-scope:v1` marker at all (heal-scope
status `missing`), authored by the intake's own account, opens a replacement
instead of a duplicate occurrence. `_heal_scope_recovery_evaluate` in
`scripts/workflow_failure_heal_intake.sh` re-runs the budget without the
original (its body never feeds the lineage) and turns the decision into an
open, so the replacement goes through the normal scope freeze, diagnosis,
scope render and the unchanged `_heal_intake_scope_guard_permits_create`.
After `gh issue create` succeeds, the original gets one comment ending in
`<!-- ai:heal-scope-recovery:v1 replaced_by=<n> repo=<owner/repo> -->` and is
closed as "not planned"; its body is never edited and its editor never runs.
Only a marker posted by the intake's account counts
(`workflow_failure_heal.py heal-scope-recovery check`): later reports on an
original that carries it go to the replacement as occurrences
(`outcome=redirect`, the close is retried). A marker present in any form,
another author, an unverified report, a failed `/user` or comment read, or a
second budget that does not open keep today's duplicate handling. A
classification that opens no issue posts the usual occurrence on the
original (`outcome=no_replacement`). Logs:
`WORKFLOW_HEAL heal_scope_recovery outcome=eligible|skip|redirect|replaced|no_replacement reason=`.
