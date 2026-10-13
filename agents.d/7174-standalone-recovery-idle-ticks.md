<!-- agents: section="Implement scope-lock label" -->
Idle-tick standalone recovery (#7174): on cron ticks with no open
`ai:orchestrator-tracking` issue, `orchestrate_poll.yml` runs "Run standalone
stall recovery without active projects" (`has_work != 'true'`, the complement
of "Process each tracking issue", every repository, `continue-on-error: true`
so a failure does not skip the latch-release and unblock-scan sweeps after
it). It carries the full poller step's env plus `STANDALONE_RECOVERY_SWEEP_ONLY=true`
(default `false` in the script), which makes `scripts/orchestrate_poll_process.sh`
prime the phase concurrency caps, run only `run_standalone_stall_recovery` and exit, so a standalone plan held
for automation paths is re-checked after its body gains a grant (the
plan-step and poller grant checks already pass `--origin-repo`). The step
skips with a warning when the staged poller lacks the mode. "Resolve AI
engine" now runs on every tick and the Claude CLI / pool / pool-alert steps
gate on `any_claude` alone, so the idle-tick stall judge resolves its engine
as on a busy tick. The OpenCode install also runs on idle ticks unless
`ENABLE_STANDALONE_STALL_RECOVERY` or `ENABLE_STALL_JUDGE` is `false`. `ENABLE_STANDALONE_STALL_RECOVERY=false` turns the
idle-tick recovery off.
