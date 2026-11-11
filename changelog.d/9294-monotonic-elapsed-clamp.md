<!-- changelog: fixed -->
- **CI module timings and the review checkout summary no longer break when the runner's clock steps back.** The "Orchestrate poll single-file module tests" step in `.github/workflows/ci.yml` and `scripts/review_autofix_step_checkout_pr_head_branch.sh` now time with milliseconds from `/proc/uptime`, which a wall-clock adjustment does not move, and fall back to the previous `date +%s%3N` reading where that file is unreadable. A negative difference now reads `0`.

A runner clock adjustment made the single-file step print `elapsed_ms=-94` on a `CI_SUBTEST_END` line and made the `REVIEW_HEAD_CHECKOUT` summary print `fetch_ms=na` for a fetch that had run, which failed two `orchestrate-poll` CI groups. Line formats, keys, exit codes and pass/fail logic are unchanged; `na` still means the operation never started.

What this means for consumer repos: nothing to change. After the next `@stable` sync, review checkout summaries keep non-negative `fetch_ms` / `switch_ms` values through clock adjustments.

Refs #9294.
