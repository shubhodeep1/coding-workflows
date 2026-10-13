<!-- agents: section="Workflow architecture" -->
Heal-scope recovery (#7191, #7323, the operator step for #7082) is gated by
`HEAL_SCOPE_RECOVERY_ENABLED`, on by default: unset, empty or `true` (any
letter case) keeps it on, `false` turns off recovery only, and any other
value also turns it off with
`::warning::WORKFLOW_HEAL heal_scope_recovery outcome=skip reason=invalid_switch`.
It does not need `HEAL_INTAKE_SCOPE_GUARD_ENABLED`. A report whose OIDC
identity verified (`report_auth=verified`) and whose fingerprint matches an
open heal issue with no `ai:workflow-heal-scope:v1` marker at all (heal-scope
status `missing`), authored by the intake's own account, opens a replacement
instead of a duplicate occurrence. `_heal_scope_recovery_evaluate` in
`scripts/workflow_failure_heal_intake.sh` re-runs the budget without the
original (its body never feeds the lineage) and turns the decision into an
open, so the replacement goes through the normal scope freeze, diagnosis and
scope render. `_heal_intake_scope_guard_permits_create` then runs its
unchanged `heal-scope verify` check for this path even while the creation
gate is off (`WORKFLOW_HEAL scope_guard outcome=recovery_only`; only
`HEAL_SCOPE_RECOVERY_ACTIVE`, which the script resets before use, selects
it), so a missing or forged scope refuses creation with
`reason=scope_marker_unverified`, a CRITICAL alert and a nonzero exit, and
the original stays open. Every other report with the gate off still ends with
`skip reason=scope_guard_disabled`.
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
