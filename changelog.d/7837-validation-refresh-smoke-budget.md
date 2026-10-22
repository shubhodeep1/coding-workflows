<!-- changelog: fixed -->
- **The stable release gate no longer fails while its validation-refresh check is still running.** The `Dispatch & watch — validation-refresh` step of `test-and-mark-stable.yml` now dispatches the smoke run with `discovery_dry_run=true` and waits as long as the dispatched job may run.

Release run 39694107256 gave up after 40 minutes on a validation-refresh run that was still `in_progress`. The gate waited 2400 s, but the refresh job's own budget is 60 minutes, and the smoke run also spent up to 35 minutes on real model discovery before the 13 consumer self-tests started. The smoke run now skips only the model call and the discovery pull request; dedup, memory plumbing and every consumer's clone, render, lint and self-test still run. The nightly refresh still runs real discovery.

| The numbers that matter | Before | After |
| --- | --- | --- |
| Watch window (`--completion-timeout-secs`) | 2400 s | 3720 s |
| Step `timeout-minutes` | 42 | 65 |
| `orphan-workflows-test` job `timeout-minutes` | 110 | 135 |

What this means for consumer repos: nothing changes for consumers; a healthy validation-refresh run no longer blocks the release that delivers their next sync.

### For contributors

`tests/test_validation_refresh_workflow_contract.py` now checks that the smoke dispatch passes `discovery_dry_run=true`, that the watch window covers `validation-refresh.yml`'s `refresh` job budget plus a minute, and that watcher < step < job, with the dispatch steps' timeouts summing below the job's.
