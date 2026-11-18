<!-- changelog: fixed -->
- **The remaining millisecond timers read bash's clock first, so their `*_ms` fields report real durations on ubuntu-26.04 runners.**

On ubuntu-26.04 runners, which ship Rust uutils coreutils, `date +%s%3N` sometimes returns a reading one digit short. PR #9240 fixed the orchestrator poller's clock; six more scripts still called `date +%s%3N` directly. `_resolver_terminal_now_ms` (`scripts/review_conflict_resolve.sh`), `_mt_now_ms` (`scripts/review_merge_train.sh`) and `review_head_checkout_now_ms` (`scripts/review_autofix_step_checkout_pr_head_branch.sh`) now read `EPOCHREALTIME` first, and the three semble scripts (`semble_helpers.sh`, `build_semble_wrapper.sh`, `install_semble.sh`) get one small clock each that does the same. `date +%s%3N` stays as the fallback for bash < 5 and is only trusted with 13 or more digits; otherwise `date +%s` times 1000 is used. `index_ms` and `SEMBLE_INSTALL_MS`, which subtracted raw readings, now clamp at 0; the merge-train `elapsed_ms`, the resolver attempt timings, the checkout timings and the SEMBLE `ms=` fields were already clamped or validated, so before this change a short reading showed up there as a wrong value, 0 or `na` rather than a negative number. Log keys, env names and output formats are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Scripts changed | 6 (`_memory_epoch_ms` already read `EPOCHREALTIME` first) |
| Fields that could print a negative value before | `index_ms`, `SEMBLE_INSTALL_MS` |
| New env vars | 0 |

What this means for consumer repos: after the next `@stable` sync, merge-train, conflict-resolver, checkout and semble timing fields report sane values on ubuntu-26.04 runners. No action needed.

Refs #9240
Refs #9246
