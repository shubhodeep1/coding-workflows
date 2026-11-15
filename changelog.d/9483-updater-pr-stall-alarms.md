<!-- changelog: fixed -->
- **Consumer update PRs no longer stall silently.** The updater refreshes its PR through the REST API, a blocked auto-merge fails the verify job with an alert, verified update PRs get the `ai-review/head-gate` success status, and the drift audit pages when a consumer stays behind stable for 24 hours.

Two consumers (btc_sweeper#264, atlas-bridge.gd#134) stayed on pre-v1.47.2 wrappers through the Python 3.14 outage. Their update PRs never merged and nothing alerted: `gh pr edit` needs the `read:org` scope `GH_PAT` lacks, so every refresh failed with `pr_edit_failed`; a failed auto-merge only logged a warning; the head gate stayed `pending` because review leaves updater PRs before its success post; and the drift audit measured staleness from when each update PR opened, so every new PR reset the clock.

| The numbers that matter | Value |
| --- | --- |
| Pin-age alarm threshold | 24 hours (`CONSUMER_PIN_MAX_AGE_SECONDS`, default 86400) |
| Drift audit schedule | every 6 hours (was daily) |
| Extra API calls per updater-PR review | at most 2 (PR read, combined status); 0 for other PRs |

What this means for consumer repos: after the next `@stable` sync, an update PR whose auto-merge cannot be enabled fails the `verify` job (the verify status stays `success`, so you can merge it by hand). Updater PRs with green `ai-update-workflows/verify` and `coding-workflows/workflow-integrity` statuses get `ai-review/head-gate` = `success`; set `REVIEW_HEAD_GATE_UPDATER_EXEMPT_ENABLED=false` to turn that off.
