<!-- changelog: fixed -->
- **The nightly validation refresh now shows why a consumer self-test failed, and gives slow monorepos 900 seconds instead of 300.** It also stops calling untracked validation files "drift".

Since the harness moved into the sandbox (`scripts/validation_harness_sandbox.sh`), the `self_test_failed` diagnostic in `scripts/validation_refresh_runner.py` kept only the sandbox's own `selfcheck ok` and `run ok staged` lines, so 11 of the 13 consumer repos showed red with no cause. The diagnostic now skips those lines and adds the driver result's phase, the first failing test and its error, the driver `log_tail`, and the tail of `compose.log`, capped at about 4 KB. Consumer text is stripped of control characters, has credential values redacted, and has lines starting with `::` or `##[` neutralised before it reaches the Actions log. The self-test also gets its own timeout, `VALIDATION_REFRESH_SELF_TEST_TIMEOUT_SECONDS` (default `900`), because the 300 second executor default killed bitsafe.io's turbo build with exit 124.

| The numbers that matter | Value |
| --- | --- |
| Self-test timeout, before | 300 s (executor default) |
| Self-test timeout, now | 900 s default, `VALIDATION_REFRESH_SELF_TEST_TIMEOUT_SECONDS`, clamped to 3600 s |
| Failure detail cap | 4096 characters, last 20 lines of each log |

What this means for operators: set `VALIDATION_REFRESH_SELF_TEST_TIMEOUT_SECONDS` as a repo variable to change the cap, and read the `self_test_failed(...)` entry in the run's summary JSON for the cause. `drifted=1` still means the clone is not clean after the render. When no file under `validation/` is tracked on a consumer's default branch, the result now ends with `validation_assets_not_committed`, which means the repo has not committed its validation assets, not that they drifted.

### For contributors

`process_repository` runs one extra `git ls-files -- validation` when the tree is dirty. `_dispatch_discovery` now checks the 7-day dedup before the discovery budget, so late repos report `skipped_dedup` instead of `skipped_budget` when a recent discovery already exists.
