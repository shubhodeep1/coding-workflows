<!-- agents: section="Workflow architecture" -->
Standalone PR sweeps without a project (#7177): the poller's standalone PR
conflict sweep and noop-suspicious recovery sweep (with their
`STANDALONE_CONFLICT_STUCK` and `NOOP_FORCE_MERGE_GATE_ALERT` alerts) run
inline at the end of "Process each tracking issue" when a tracking issue is
open. On ticks without one, the `orchestrate_poll.yml` step "Run standalone PR
sweeps without active projects" runs `scripts/orchestrate_poll_process.sh` with
`STANDALONE_PR_SWEEP_ONLY=true` (default `false`). That mode skips the tracking
loop, `prime_phase_concurrency_snapshot`, standalone stall recovery, the
staged-support latch release, the unblock scan and `close_merged_issues_sweep`.
The complementary `has_work` gates keep the two paths exclusive. The step skips
with a warning when the staged poller lacks the mode, is `continue-on-error`,
and forwards `ALLOW_WORKFLOW_EDITS`. "Stage workflow support files" also stages
`validate_editor_audit.sh` (optional), which the no-op sweep's force-merge
gate B needs; without it gate B fails closed. Both sweeps send Telegram
alerts at `STANDALONE_SWEEP_ALERT_MSG_LEVEL` (from `vars.ALERT_MSG_LEVEL`, default
`WARNING`) instead of inheriting `find_tracking`'s all-smoke-fixture `SILENT`,
then restore `ALERT_MSG_LEVEL`. An empty value keeps the poller level, the value is
matched case-insensitively, and a value outside
`DEBUG|WARNING|ERROR|CRITICAL|SILENT` is ignored with a warning.
