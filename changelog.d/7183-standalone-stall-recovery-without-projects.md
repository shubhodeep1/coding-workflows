<!-- changelog: fixed -->
- **Standalone stall recovery now runs when no orchestrator project is active.** Before, a repository with no open `ai:orchestrator-tracking` issue never ran it, so stalled standalone issues stayed stuck.

On those ticks the poller toolchain steps were skipped, so the "Run standalone stall recovery without active projects" step had no installed tools to run with. `.github/workflows/orchestrate_poll.yml` now installs the toolchain on no-project ticks. The step runs the poller with `STANDALONE_RECOVERY_SWEEP_ONLY=true`, which runs only standalone stall recovery, and it runs only when the API key check and support staging succeeded. The latch release and unblock scan still run if recovery fails. They do not run if the tracking-issue lookup failed.

| The numbers that matter | Value |
| --- | --- |
| Kill switch | `ENABLE_STANDALONE_STALL_RECOVERY=false` (default `true`) |
| Extra GitHub API calls per idle 5-minute tick | about 10 |

What this means for operators: idle ticks now install the poller toolchain and make recovery's usual API calls. Set `ENABLE_STANDALONE_STALL_RECOVERY=false` to turn both off. The merged-issue close sweep still runs only while a project is active.
