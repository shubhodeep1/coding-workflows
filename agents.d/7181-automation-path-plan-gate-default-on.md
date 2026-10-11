<!-- agents: section="Implement scope-lock label" -->
Since #7181 the automation-path plan approval gate is on by default:
`AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` defaults to `true` in
`plan.yml`, in `orchestrate_poll.yml` and in
`scripts/orchestrate_poll_process.sh` (`_automation_path_plan_auto_approval_gate`).
Only `false`, `0`, `no` or `off` (any letter case) turn it off; an unset,
empty or unrecognised value keeps it on (fail closed). Hold markers
(`ai:automation-path-plan-hold:v1`) only deduplicate the hold comment and no
longer activate the gate. Managed and standalone stall-recovery `auto_approve`
both run it; the guard, extractor, marker and log formats are unchanged.
