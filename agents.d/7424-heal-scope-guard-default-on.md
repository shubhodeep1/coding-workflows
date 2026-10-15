<!-- agents: section="Workflow architecture" -->
Heal intake reports with no runs (#7424): a report that claimed no run at all
(a label-only report such as `ai:scope-blocked`, or `engine_fallback`) has no
verified run to bound a heal scope with. Both the pre-compose scope-marker
guard (`WORKFLOW_HEAL_SCOPE_MARKER_GUARD_ENABLED`) and the creation gate
(`HEAL_INTAKE_SCOPE_GUARD_ENABLED`) skip it with
`WORKFLOW_HEAL skip reason=scope_unavailable_no_runs stage=marker_guard|scope_guard`
and exit 0, with no CRITICAL alert (`_heal_report_claimed_no_runs` in
`scripts/workflow_failure_heal_intake.sh`). The raw report must name no run as
well: a report whose claimed runs `validate-payload` dropped (malformed or
another repository) keeps the CRITICAL refusal, and so does a heal-scope
recovery replacement (`HEAL_SCOPE_RECOVERY_ACTIVE=true`).
