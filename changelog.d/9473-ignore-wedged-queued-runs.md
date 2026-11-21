<!-- changelog: fixed -->
- **Wedged queued workflow runs no longer block dispatch.** GitHub left hundreds of runs in `status=queued` with zero jobs, and refuses to cancel, force-cancel or delete them (run `43095142301`: 409, 409, 403). Every guard that asks "is a run already active?" treated them as active forever.

A queued run whose `created_at` is older than `ZOMBIE_RUN_MAX_QUEUED_HOURS` (default `24`) is now ignored by the poller's active-run guards, the review/autofix peer check, the merge-train release, the review sweep (as a floor beneath `SWEEP_STALE_QUEUED_MINUTES`), the stable release and promote cycles, the analysis apply gate, the worktree registry, the CI cancelled-run rerun, the unblock scan and the stable gate's plan-run check. The rule uses the run listings each guard already fetched, so it adds no API calls. Each ignored run logs `ZOMBIE_RUN_IGNORED run=<id> workflow=<path> age_h=<n> guard=<guard>` once per guard. `in_progress`, `pending`, `waiting` and `requested` runs never age out, and a run with a missing or unparseable `created_at` still counts as active.

What this means for consumer repos: after the next `@stable` sync the filter is on with the 24-hour default. Set the repository variable `ZOMBIE_RUN_MAX_QUEUED_HOURS=0` to turn it off.

Refs #9473.
