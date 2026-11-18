<!-- changelog: changed -->
- **The release-gate dispatcher closes the previous cycle's fixtures and waits for the runner queue before it retries.** A gate that failed while the Actions queue was deep is no longer re-dispatched into the same queue every 6 hours.

`scripts/auto_release_stable.sh`, run every 6 hours by `.github/workflows/auto-release-stable.yml`, now does two things before it dispatches `test-and-mark-stable.yml`. It closes the open `[E2E ...]` fixture issues and PRs left behind by gate runs that finished without success (`AUTO_RELEASE_FIXTURE_CLEANUP`); orchestrator fixture projects stay with the poller's `SMOKE_FIXTURE_REAP`. Then, if the newest completed gate run on `stable` failed less than 24 hours ago, it reads the repository's queued-run count and skips with `AUTO_RELEASE_SKIPPED reason=runner_queue_deep` while 100 or more runs are queued. The skipped tick spends none of the `AUTO_RELEASE_STABLE_MAX_ATTEMPTS` budget. After 24 hours one cycle is dispatched whatever the queue, so a release is delayed, never held for good. Both steps fail open: an unreadable queue count or search dispatches as before.

| The numbers that matter | Value |
| --- | --- |
| `Test & Mark Stable Release` runs, 2026-11-10 to 2026-11-17 | 16 (2 passed, 9 failed, 5 cancelled) |
| Fixtures filed by those cycles | 79 issues, 29 PRs |
| `AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS` | `100` (`0` disables the back-off) |
| `AUTO_RELEASE_STABLE_QUEUE_BACKOFF_HOURS` | `24` |
| `AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_ENABLED` / `AUTO_RELEASE_STABLE_FIXTURE_CLEANUP_MAX` | `true` / `30` |
| Gate cycles per day into a deep queue | at most 1 (was up to 4) |
| Extra API calls per tick | 0 when no gate run in the listing failed; otherwise 1 `user` read and 2 searches for the cleanup, 1 queue count for the back-off |

What this means for operators: while 100 or more runs are queued after a failed gate, the 6-hour tick logs `runner_queue_deep` and releases nothing, for up to 24 hours. To release sooner, dispatch `test-and-mark-stable.yml` by hand or set the repo variable `AUTO_RELEASE_STABLE_MAX_QUEUED_RUNS=0`. Fixtures are closed without a comment; the dispatcher's log names each one.

### For contributors

The daily promote cycle (`scripts/promote_main_cycle.sh`) dispatches its own `gate_only` run and has no back-off. `tests/test_auto_release_stable.py` covers the back-off window, its fail-open paths, the cleanup's selection rules and the workflow's env wiring.
