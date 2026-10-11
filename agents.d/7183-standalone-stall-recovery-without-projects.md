<!-- agents: section="Workflow architecture" -->
Standalone stall recovery on ticks with no active project (issue #7183):
when `orchestrate_poll.yml` finds no `ai:orchestrator-tracking` issue,
`find_tracking` sets `has_work=false` and `standalone_recovery=true` (unless
`ENABLE_STANDALONE_STALL_RECOVERY` is off). The poller toolchain steps then
still run, and "Process each tracking issue" runs the poller with
`STANDALONE_STALL_RECOVERY_SWEEP_ONLY=true`, which primes the phase
concurrency caps, runs `run_standalone_stall_recovery` and exits before the
per-project loop and the post-loop sweeps. It runs only when
"Validate required environment variables" (`validate_env`) and
"Stage workflow support files" (`stage_support`) succeeded; both are
`continue-on-error` on no-project ticks, so a missing key or staging failure
skips recovery without failing the tick. A staged poller without the mode
logs a warning and runs nothing. The latch release and unblock scan steps
that follow are gated on `!cancelled()`, so they still run when recovery
fails. `close_merged_issues_sweep` and the standalone PR conflict sweep still
run only on ticks with an active project.
