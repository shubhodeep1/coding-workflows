<!-- changelog: changed -->
- **Integration branches sync from `main` only when needed, the review sweep stops replacing reviews that are only waiting for a runner, and the one-off historical triage workflow is gone.** These cut full CI runs and duplicate review runs while the runner pool is backed up.

The orchestrator poller used to merge `main` into every open `orchestrator/project-*` branch on every tick. Each push started two full CI runs, so on 2026-11-12 project 8969 alone synced 13 times. A tick now syncs only when one of these holds:

- the last sync is older than `INTEGRATION_SYNC_MIN_INTERVAL_SECS` (default 24 hours);
- it was not clean;
- the integration PR reports a conflict.

In addition, one sync is forced right before the next wave is dispatched and right before a finished project starts its security pass.

The poller logs `INTEGRATION_SYNC_THROTTLE` lines. `review_autofix_sweep.yml` still treats a review run queued past `SWEEP_STALE_QUEUED_MINUTES` as wedged, but now only when GitHub created no jobs for it (`SWEEP_STALE_QUEUED_REQUIRE_NO_JOBS`, default `true`). Before, it replaced about 86 waiting reviews a day with pending duplicates. `.github/workflows/check-failure-triage-historical.yml`, which ran on every push to `main` and always skipped after its issue #7300 closed, is removed with its test, registry entry and docs.

| The numbers that matter | Value |
| --- | --- |
| Pushes to `orchestrator/project-8969` on 2026-11-12, all `chore: sync main` | 13 |
| `INTEGRATION_SYNC_MIN_INTERVAL_SECS` default | `86400` (`0` = sync every tick) |
| Sweep dispatches replaced while pending (2026-11-12) | 86 |
| Extra API reads | 1 per stale queued review run (sweep); 1 final-PR read per would-be skip with a final PR (poller) |

What this means for operators and consumer repos: integration branches pick up `main` at least daily, before every new wave and before the final security pass, and a conflict on the integration PR still triggers a sync at once. Set `INTEGRATION_SYNC_MIN_INTERVAL_SECS=0` to go back to syncing every tick. No review is lost: a waiting review run reviews the PR's current head when it starts. Consumers receive the poller and sweep changes on their next `@stable` sync.

### For contributors

The throttle is `_integration_sync_throttle_reason` in `scripts/orchestrate_poll_process.sh`. The security-pass caller passes `force`. Tests are in `tests/test_security_pass_final_pr_sync.py` and `tests/test_review_autofix_sweep_stale_queued.py`.
