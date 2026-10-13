<!-- changelog: fixed -->
- **The standalone PR conflict sweep and the no-op recovery sweep now run on every poll tick, including ticks with no open orchestrator project.** Their stuck-PR alerts also no longer go silent when the only open project is the release gate's smoke fixture.

PR #7156 added two once-per-head alerts for standalone PRs: one after repeated merge-conflict resolution attempts, and one when the no-op force-merge fallback stops at a gate. Both sweeps ran only inside the main poller, and `orchestrate_poll.yml` runs that poller only when an `ai:orchestrator-tracking` issue is open. In a repository with no active project, a conflicted or stuck standalone PR was therefore never swept and never alerted. When every open tracking issue was a smoke fixture, the workflow also set `ALERT_MSG_LEVEL=SILENT`, which silenced the same alerts for real PRs.

On ticks with no tracking issue, a new step, "Run standalone PR sweeps without active projects", now runs `scripts/orchestrate_poll_process.sh` with `STANDALONE_PR_SWEEP_ONLY=true`. That mode runs only the two sweeps. Ticks with projects keep running the sweeps inline, so each tick sweeps once. The new step honours `ALLOW_WORKFLOW_EDITS`, and the poll workflow now also stages `scripts/validate_editor_audit.sh`, which the no-op sweep needs before it can fall back to a force-merge.

Both sweeps now page at `STANDALONE_SWEEP_ALERT_MSG_LEVEL`, which the workflow sets from `vars.ALERT_MSG_LEVEL` (default `WARNING`). The level then goes back to the poller's own setting. An empty value keeps the poller level, and a value that is not a known level is ignored with a warning.

What this means for operators: standalone PRs are swept every 5 minutes whether or not a project is open, at the cost of one sweep's GitHub API reads per idle tick. A consumer whose pinned support commit predates this change logs a warning and skips the new step until its next sync.
