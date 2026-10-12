<!-- agents: section="Implement scope-lock label" -->
Plan auto-approval gate (#6769, replacing the #6838 gate): with
`AUTOMATION_PATH_PLAN_AUTO_APPROVAL_ENABLED` off (the default), no automatic
`/approved` is posted for a plan that lists automation paths. This covers
`plan.yml`'s "Auto-approve clear plan" step, both stall-recovery `auto_approve`
arms, and both `retrigger_implement` arms while the issue is in
`ai:awaiting-approval`. With the flag on, every listed protected path needs an
exact trusted `files_touched` grant; the open pipeline-author grant does not
count. `files_touched_scope_guard.py --plan-auto-approval-gate` decides (exit 0
approves, 30 holds, every error holds). Plan paths are taken whole up to
whitespace, a backtick or a quote, so an unusual filename is never cut to a
grantable prefix. The poller reads only the newest
`<!-- ai:plan-proposal:v1 -->` comment by the `GH_PAT` login (none found holds
with `plan_comment_unavailable`), and the batched issue GraphQL now carries
`author_login` / `author_association`. A `plan.yml` hold posts one deduplicated
`<!-- ai:automation-path-plan-hold:v1 ... -->` comment and a WARNING; the
poller only logs `AI_PHASE_GATE_V1 phase=stall_recovery ... outcome=defer` and
`STALL_SKIP ... reason=automation_path_plan_hold` without counting a recovery
attempt. The implement-time guard and the staged-support latch release are
unchanged.
