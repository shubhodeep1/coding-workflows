<!-- changelog: fixed -->
- **Standalone stall recovery now also runs when no orchestrator project is open.** A standalone plan held for automation paths is approved once its issue grants them, even with no tracking issue open.

Activation verification of PR #6849 found that `orchestrate_poll.yml` ran standalone stall recovery only inside the full poller, which runs only while a tracking issue is open. A new step, "Run standalone stall recovery without active projects", runs on the other ticks with `STANDALONE_RECOVERY_SWEEP_ONLY=true` and the full poller's settings. A failure in that step does not stop the latch-release and unblock-scan steps that follow it. The other gap in that report, passing `--origin-repo` to the plan-step and poller grant checks, was already fixed on `main`.

| The numbers that matter | Value |
| --- | --- |
| Idle-tick recovery cadence | every poll tick (5 minutes) |
| Turn it off | `ENABLE_STANDALONE_STALL_RECOVERY=false` |

What this means for operators: in a repository whose stall judge runs on Claude, idle ticks now also install the Claude CLI and probe the account pool. Set `ENABLE_STALL_JUDGE=false` or `ENABLE_STANDALONE_STALL_RECOVERY=false` to avoid that cost.
