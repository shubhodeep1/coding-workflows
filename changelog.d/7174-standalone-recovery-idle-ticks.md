<!-- changelog: fixed -->
- **Standalone stall recovery now also runs when no orchestrator project is open, and plan auto-approval verifies where a pipeline-written issue came from.** A standalone plan held for automation paths is approved once its issue grants them, even with no tracking issue open.

Activation verification of PR #6849 found two gaps. First, the plan step and the poller built the automation-path grant without `--origin-repo`, so any pipeline-written re-issue or follow-up that names a source issue was held as `origin_unverified`, even when a maintainer wrote the source. Both now pass the run's repository, as `implement.yml` already did. Second, `orchestrate_poll.yml` ran standalone stall recovery only inside the full poller, which runs only while a tracking issue is open. A new step, "Run standalone stall recovery without active projects", runs on the other ticks with `STANDALONE_RECOVERY_SWEEP_ONLY=true` and the full poller's settings.

| The numbers that matter | Value |
| --- | --- |
| Extra issue reads per grant check | at most 8, only for a pipeline-written issue that names a source |
| Idle-tick recovery cadence | every poll tick (5 minutes) |
| Turn it off | `ENABLE_STANDALONE_STALL_RECOVERY=false` |

What this means for operators: in a repository whose stall judge runs on Claude, idle ticks now also install the Claude CLI and probe the account pool. Set `ENABLE_STALL_JUDGE=false` or `ENABLE_STANDALONE_STALL_RECOVERY=false` to avoid that cost.
