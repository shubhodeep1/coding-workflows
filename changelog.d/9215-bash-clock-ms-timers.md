<!-- changelog: fixed -->
- **CI on ubuntu-26.04 runners passes again: the closed-reader `gh_retry` test no longer fails on Rust coreutils, and the millisecond-clock change is covered by a test.**

`tests/test_gh_retry_stdout_isolation.py::test_closed_reader_does_not_rerun_a_successful_command` now writes more output than any pipe can hold, into a reader that takes one byte and exits. Rust coreutils' `cat` (ubuntu-26.04) enlarges its stdout pipe to `pipe-max-size` and splices up to 1 MiB in one call, so the old 1 MiB output fit in the pipe and the test failed on every `main` run even though `gh_retry` behaved correctly. A new test in `tests/test_ci_poll_test_sharding.py` checks that the CI single-file step's timer ignores a bad `date` reading.

The switch of the remaining `date +%s%3N` timers to bash's `EPOCHREALTIME` already shipped with `changelog.d/9252-epochrealtime-ms-clock.md`. This change only extends the clock comments in four of those scripts: on the ubuntu-26.04 image `date +%s%3N` can also return readings that go backwards within one run, which made `switch_ms` in the `REVIEW_HEAD_CHECKOUT` line come out `na` and failed `tests/test_review_checkout_head_diagnostics.py` intermittently.

| The numbers that matter | Value |
| --- | --- |
| Tests fixed or added | 2: the closed-reader `gh_retry` test (fixed), the single-file step clock test (added) |
| Scripts changed | 4, comments only: `review_autofix_step_checkout_pr_head_branch.sh`, `semble_helpers.sh`, `install_semble.sh`, `build_semble_wrapper.sh` |
| Log keys, env names, output formats changed | 0 |

What this means for operators: nothing to do. `main`'s CI stops failing on ubuntu-26.04 runners, so pull requests can merge again.
