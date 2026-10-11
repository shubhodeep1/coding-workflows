<!-- agents: section="Implement scope-lock label" -->
`AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` (default `false`, #6838) moves
the same grant check in front of the automatic `/approved`. When on,
`plan.yml`'s "Auto-approve clear plan" step and both poller stall-recovery
`auto_approve` branches extract the automation paths from the plan's
"Files ... change" section. They then run the unchanged
`files_touched_scope_guard.py --emit-automation-grant` /
`--check-automation-paths` from the verified support checkout. If any listed
path is ungranted, or the check cannot run, the issue stays in
`ai:awaiting-approval` with one `<!-- ai:automation-path-plan-hold:v1 ... -->`
comment and a WARNING. The poller logs `STALL_SKIP ...
reason=automation_path_ungranted|automation_path_check_unavailable` without
counting a recovery attempt. The poller also turns the gate on for an issue
whenever a trusted hold marker is newer than the latest trusted plan comment,
because `orchestrate_poll.yml` does not forward the variable. The extractor
block is byte-identical in both files, and
`tests/test_plan_auto_approve_safe_state_fetch.py` pins that.
