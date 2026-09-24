<!-- changelog: fixed -->
- **Workspace-guard failures no longer stall unrelated review-blocked issues.** The poller skips the affected PR, alerts, cleans its workspace, and continues other issues in the same tick. A rejected conflict-resolver attempt still exits before parsing model output and now records its terminal failure substate.
