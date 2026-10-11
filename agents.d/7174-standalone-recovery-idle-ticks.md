<!-- agents: section="Implement scope-lock label" -->
Automation-path grant source chain and idle-tick recovery (#7174): all three
`--emit-automation-grant` sites (`implement.yml`, `plan.yml`'s "Auto-approve
clear plan" and the poller's `_automation_path_plan_auto_approval_gate`) pass
`--origin-repo "${GITHUB_REPOSITORY}"`, so a pipeline-authored issue that
names a source issue gets its source chain verified at plan time and on stall
recovery instead of `origin_unverified`. On cron ticks with no open
`ai:orchestrator-tracking` issue, `orchestrate_poll.yml` runs "Run standalone
stall recovery without active projects" (`has_work != 'true'`, the complement
of "Process each tracking issue", every repository). It carries the full
poller step's env plus `STANDALONE_RECOVERY_SWEEP_ONLY=true` (default `false`
in the script), which makes `scripts/orchestrate_poll_process.sh` run only
`run_standalone_stall_recovery` and exit, so a standalone plan held for
automation paths is re-checked after its body gains a grant. The step skips
with a warning when the staged poller lacks the mode. "Resolve AI engine" now
runs on every tick and the Claude CLI / pool / pool-alert steps gate on
`any_claude` alone, so the idle-tick stall judge resolves its engine as on a
busy tick. `ENABLE_STANDALONE_STALL_RECOVERY=false` turns the idle-tick
recovery off.
