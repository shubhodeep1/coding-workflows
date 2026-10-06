<!-- changelog: changed -->
- **Reduced shared GitHub PAT traffic and made failed clarification routing recover automatically.** High-volume jobs report start/end PAT quota snapshots (shared-budget deltas are estimates), and the poller batches clean-PR reads and skips draft Claude PRs. Review watchdogs and comment pagination avoid redundant requests. A trusted failed `/reclarify` is requeued by the existing poller only after the PAT budget recovers; the failure path creates the discovery label if it has not yet been synced.

Operator option: after measuring a full day of job-level quota estimates, consider isolating low-volume routing from high-volume review on separate credentials if contention persists. This change keeps one PAT and does not modify repository secrets.
