<!-- changelog: fixed -->
- **Standalone stall recovery now runs when no orchestrator project is active.** Before, a repository with no open `ai:orchestrator-tracking` issue never ran it, so stalled standalone issues stayed stuck.

The poller's "Process each tracking issue" step was skipped on those ticks, and the two steps that did run (staged-support latch release and the unblock scan) exit before `run_standalone_stall_recovery`. `.github/workflows/orchestrate_poll.yml` now runs that step on no-project ticks with `STANDALONE_STALL_RECOVERY_SWEEP_ONLY=true`, which runs only standalone stall recovery. The latch and unblock steps still run if recovery fails.

| The numbers that matter | Value |
| --- | --- |
| Kill switch | `ENABLE_STANDALONE_STALL_RECOVERY=false` (default `true`) |
| Extra GitHub API calls per idle 5-minute tick | about 10 |

What this means for operators: idle ticks now install the poller toolchain and make recovery's usual API calls. Set `ENABLE_STANDALONE_STALL_RECOVERY=false` to turn both off. The merged-issue close sweep and the standalone PR conflict sweep still run only while a project is active.
