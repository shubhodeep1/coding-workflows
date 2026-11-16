<!-- agents: section="Workflow architecture" -->
Integration sync throttle (run-volume plan item 3, `docs/plans/reduce-actions-run-volume-plan.md`). The poller's per-tick `main` → `orchestrator/project-*` sync (`sync_default_into_integration_branch` in `scripts/orchestrate_poll_process.sh`) now asks `_integration_sync_throttle_reason` first, right before the merges API call and after the superseded-by-main checks. A tick merges `main` in only when one of these holds:

- the project has no recorded sync (`never_synced`);
- `.sync.last_default_sync_at` is older than `INTEGRATION_SYNC_MIN_INTERVAL_SECS` (default `86400`; `interval_elapsed`);
- the last sync is not clean, so conflicts, failures and healing retry every tick (`not_clean`);
- every issue in the current wave is merged or skipped, so the judge, the next-wave dispatch or the final merge comes next (`wave_complete`);
- the recorded final PR is open with `mergeable: false` (`final_pr_unmergeable`).

`0` disables the throttle (`interval_disabled`). The security-pass helper `security_pass_sync_final_pr_if_unmergeable` passes `force`, because it has already found the final PR unmergeable. An unreadable clock or final PR fails open to a sync. Every successful merges call records the clock. A 201 (merge commit) persists it with the state comment at once; a 204 no-op rides the next state write, so a no-op sync edits no comment. Before this change one project synced 13 times on 2026-11-12, and each push started two full CI runs. `tests/test_security_pass_final_pr_sync.py` pins every reason.

<!-- agents: section="Review pipeline consolidator + ledger contract" -->
- The sweep's stale-queued rule (`SWEEP_STALE_QUEUED_MINUTES`) now also needs zero jobs. With `SWEEP_STALE_QUEUED_REQUIRE_NO_JOBS` (default `true`), a `queued` review run past the age cutoff counts as wedged only when `GET actions/runs/<id>/jobs?per_page=1` reports `total_count: 0`, the shape of the wedged runs the cutoff was built for (#3841) and of the 2026-11-11 ghost runs.
  - A run with jobs is waiting for a runner. It keeps suppressing the dispatch and logs `AUTOFIX_SWEEP_STALE_QUEUED … jobs=<n> outcome=waiting_for_runner`. In the 2026-11 backlog every review run queued longer than two hours, so the age-only rule replaced each one with a pending duplicate on every tick.
  - An unreadable reply keeps the wedged path, and `false` restores the age-only rule.
  - `tests/test_review_autofix_sweep_stale_queued.py` (`SweepStaleQueuedRequiresNoJobsTest`) runs the shipped step against a fake `gh`.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `INTEGRATION_SYNC_THROTTLE` (`scripts/orchestrate_poll_process.sh` `sync_default_into_integration_branch`: `tracking_issue= branch= outcome=sync|skip reason=forced|interval_disabled|not_clean|never_synced|clock_unreadable|interval_elapsed|wave_complete|final_pr_unmergeable|final_pr_unreadable|within_interval`)
LOG_PREFIX.name=INTEGRATION_SYNC_THROTTLE
