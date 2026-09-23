<!-- changelog: fixed -->
- **Workflow failure heal reports preserve harness diagnostics.** Label-triggered reports now prioritize recent comments, compact complete orchestrator-state snapshots, and retain explicit validation run links. The poller posts harness-error detail before applying `ai:harness-broken`, preventing the reporter from racing ahead of the evidence.
