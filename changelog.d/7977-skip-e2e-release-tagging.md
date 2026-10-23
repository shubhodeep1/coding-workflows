<!-- changelog: fixed -->
- **`test-and-mark-stable.yml` now tags a release when dispatched with `skip_e2e=true`.** Before, that path always ended with the `release` and `sync-to-main` jobs skipped, so nothing was tagged even though the run reported success.

The `release` job was gated on `success()`, which GitHub evaluates as false whenever any ancestor job was skipped, and `skip_e2e` skips every live e2e job. On 2026-10-23 run 39955031510 passed `validate` and concluded `success`, but the latest tag stayed v1.38.2. `release` now checks `!cancelled()` plus explicit `source`, `resolve-version` and `validate` results, and `sync-to-main` checks `!cancelled()` before its existing conditions.

| The numbers that matter | Value |
| --- | --- |
| Jobs whose gate changed | `release`, `sync-to-main` |
| Still never tags | `gate_only` runs, `dry_run` runs, cancelled runs, any run where `validate` did not succeed |

The release Telegram notification now counts a skipped e2e smoke test as passed on a `skip_e2e=true` run that is not a dry run, so a release that published is reported as SUCCEEDED (with `e2e ⊘`) instead of FAILED.

What this means for operators: the documented emergency path ("Skip E2E test and only mark stable") works again. A full-gate run whose e2e jobs fail still skips `validate` and therefore still does not tag.
