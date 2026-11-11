<!-- changelog: fixed -->
- **Millisecond timers read bash's own clock on ubuntu-26.04 runners.** The remaining `date +%s%3N` timers now use `EPOCHREALTIME` first, so duration fields no longer come out negative or `na`.

On the ubuntu-26.04 runner image, `date +%s%3N` (Rust coreutils) sometimes returns readings that go backwards within one run. That made `switch_ms` in the `REVIEW_HEAD_CHECKOUT` line come out `na` and failed `tests/test_review_checkout_head_diagnostics.py` intermittently in CI. The fix follows the one `scripts/orchestrate_poll_process.sh` already uses: bash's `EPOCHREALTIME` first, `date +%s%3N` and then `date +%s` as fallbacks.

| The numbers that matter | Value |
| --- | --- |
| Scripts changed | 6: `review_autofix_step_checkout_pr_head_branch.sh`, `review_merge_train.sh`, `review_conflict_resolve.sh`, `semble_helpers.sh`, `install_semble.sh`, `build_semble_wrapper.sh` |
| Log fields affected | `REVIEW_HEAD_CHECKOUT` `fetch_ms` / `switch_ms` / `duration_ms`, `MERGE_TRAIN_RELEASE_SUMMARY` `elapsed_ms`, resolver terminal `attempt_ms`, `SEMBLE_BOOTSTRAP` `install_ms` / `index_ms`, Semble query `ms` |

What this means for operators: timing fields in these log lines are trustworthy again on ubuntu-26.04, and the CI test that kept failing on them stops failing.
