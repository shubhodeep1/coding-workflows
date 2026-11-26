<!-- changelog: changed -->
- **A push to `main` runs a delta suite; the full suite runs on `main` every 6 hours.**

Every push to `main` used to run the full CI suite (about 47 jobs and 500 runner-minutes), competing with final CI and review for the account's 40 concurrent job slots. A push to `main` now runs `static-checks`, the new `tests-main-delta` job and `lint`. `tests-main-delta` (`scripts/ci_main_delta.sh`) tests every change since `main`'s last green CI run with the tests the pre-push selector picks, plus the CI-structure tests. When that selection cannot be trusted (a failed read, a red last run, a selector error, more than `MAIN_PUSH_DELTA_MAX_TEST_FILES` test files, a sharded test or a timeout), the full suite runs in the same run, so an unknown selection never posts `ci/final` success. The full suite also runs on `main` at 04:30, 10:30, 16:30 and 22:30 UTC, the last one before the nightly promotion. `ci/final` on `main` now says `(delta suite)` or `(full suite)`. Check-failure triage, its resolution gate, the final-CI sweep, heal-stale and heal intake also read the scheduled runs, so a failed scheduled run counts as a red base.

| The numbers that matter | Value |
| --- | --- |
| Jobs per push to `main` | about 47 before, 4 after (with `budget-watch`) |
| Full runs on `main` per day | about 25 before (measured), 4 after |
| New API calls | 1 per push to `main` (the runs read); none in the base-result readers |

Rollback: set the repository variable `MAIN_PUSH_DELTA_CI_ENABLED=false`.
