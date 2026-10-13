<!-- agents: section="Workflow architecture" -->
Standalone stall recovery on ticks with no active project (issue #7183):
when `orchestrate_poll.yml` finds no `ai:orchestrator-tracking` issue,
`find_tracking` sets `has_work=false` and `standalone_recovery=true` (unless
`ENABLE_STANDALONE_STALL_RECOVERY` is off). The poller toolchain steps then
still run, and "Run standalone stall recovery without active projects" runs
the poller with `STANDALONE_RECOVERY_SWEEP_ONLY=true`, which primes the phase
concurrency caps, runs `run_standalone_stall_recovery` and exits before the
per-project loop and the post-loop sweeps. That step runs only when
"Validate required environment variables" (`validate_env`) and
"Stage workflow support files" (`stage_support`) have outcome `success`; both
are `continue-on-error` on no-project ticks, so a missing key or a staging
failure skips recovery without failing the tick, and `validate_env` is
skipped when the kill switch is off. A staged poller without the mode logs a
warning and runs nothing. The latch release and unblock scan steps that
follow are gated on `!cancelled()` plus a successful `find_tracking`, so
they still run when recovery or a toolchain step fails, but not when the
tracking-issue lookup itself failed. `close_merged_issues_sweep` runs only
when the full poller runs: on ticks with an active project, or when the #7284
standalone probe sets `has_work=true`.
