<!-- changelog: fixed -->
- **Stall recovery no longer escalates to a human on a stale actions-runs snapshot.** Before stall recovery builds its active issue set, the poller now checks that the per-tick actions-runs blob contains its own run. If the run is missing and the blob did not come from a confirmed live fetch, the poller refetches live once. If that refetch also fails, it holds `escalate_human` for that tick.

binance-blessings #557 and #559 were escalated ("stuck in ai:done") while their AI Review runs were still running. Each time the stall check printed `Active issue set is empty (cache: total=0, ...)`, which cannot be a live answer because the poller's own run is always `in_progress`. That blob came from the TTL cache, the ETag-304 reuse path or a fallback. In addition, the `fetch_unconfirmed` warning from the first fetch of each tick was thrown away by `prime_phase_concurrency_snapshot`.

Changes in `scripts/orchestrate_poll_process.sh`:
- `_load_actions_runs_cached` records the source of each blob (`ttl_hit`, `etag_304`, `live`, `fallback`, `no_repo`) and prints one `ACTIONS_RUNS_SOURCE` line on stderr with the counts and whether the current run is in the blob.
- The new `_ensure_actions_runs_blob_has_self_run` runs before `build_active_issue_set` in both the tracking stall block and `run_standalone_stall_recovery`. At most once per tick, it refetches live (TTL skipped, no `If-None-Match`) when the blob lacks `GITHUB_RUN_ID` and is not live. It logs its result as `ACTIONS_RUNS_SELF_CHECK`.
- When the refetch is not confirmed, `execute_stall_recovery_action` and the standalone direct dispatch print `STALL_SKIP ... reason=active_set_unverifiable` and skip `escalate_human`. All other stall actions run unchanged.
- `prime_phase_concurrency_snapshot` now forwards the loader's stderr when the load succeeds.

| The numbers that matter | Value |
| --- | --- |
| Extra live fetches per tick | at most 1 (3 REST reads), and only when the self-run check fails on a non-live blob |
| Stall thresholds / judge prompt changed | none |

What this means for consumer repos: after the next `@stable` sync, poller logs show `ACTIONS_RUNS_SOURCE` and `ACTIONS_RUNS_SELF_CHECK` lines. An issue whose active set cannot be verified is no longer escalated during that tick, and the next tick tries again.
