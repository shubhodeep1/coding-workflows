<!-- changelog: fixed -->
- **Plan done-checks no longer replace the pre-push gate's own test selection.** `run --tests-file` again adds a plan's done-check targets to the affected tests, and the heal reproduction runner's "run only this list" mode moves to a new `--tests-only` option.

The merge of PR #8436 (project #8434) into `main` gave `--tests-file` the heal runner's meaning: run only the listed tests. The implement, review-autofix and conflict-resolver gates pass a plan's done-check through that option, so whenever a plan had a done-check, the gate ran only those targets and skipped the change's affected tests, registry checks and static checks. A bad done-check file also failed the whole gate run instead of being skipped. Now `prepush_affected_tests.py run --tests-file` and `scripts/prepush_test_gate_run.sh --tests-file` add targets (copied to `/inputs/tests.txt`, a bad file logged as `reason=tests_file_rejected` and the run goes ahead). `--tests-only` keeps the #8461 replace behaviour, validation and synthetic `tests_file_*` results unchanged, and `scripts/heal_reproduce.sh` now passes it. Result keys, reason tokens and log lines are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Done-check callers restored | 3 (`implement_step_prepush_test_gate.sh`, `review_autofix_step_prepush_test_gate.sh`, `review_conflict_resolve.sh`) |
| Callers moved to `--tests-only` | 1 (`heal_reproduce.sh`) |
| First release with the bad behaviour | v1.46.0 |

What this means for consumer repos: after the next `@stable` sync, pre-push gate runs on plans with a done-check run the full affected-test selection again. Callers outside this repo that relied on `--tests-file` to replace the selection must switch to `--tests-only`.
