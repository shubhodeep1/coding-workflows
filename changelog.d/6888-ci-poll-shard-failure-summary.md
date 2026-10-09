<!-- changelog: changed -->
- **A failed orchestrate-poll test shard now names its failing tests at the end of the step log.** Issue #6888.

Each shard's log is printed inside a collapsed `::group::`, so when one shard failed, the end of the job log showed only the last shard's result. In run 37907017153 that was `33 passed`, and the failing test in shard 1 could not be identified from the triage evidence. After the shard loop, the step now prints one `::error::orchestrate-poll failing test: shard <n>: …` line per failing test, just before the existing `<N> orchestrate-poll shard(s) failed.` line:
- It prints the runner's `FAIL  <test>: <error>` line when the shard has one.
- Otherwise it prints the runner's `TEST_CASE_EVENT` line for the failed test.
- Otherwise it prints `exit <rc>, no FAIL line recorded (runner likely died)`, or `no log recorded` for a shard that never started.

At most 20 lines are printed, followed by an `... and <k> more` line when there are more. Each line is cut to 400 characters, with carriage returns removed and `%` escaped so a test message cannot break the annotation. Exit codes, failure counting and the test split are unchanged.

The same change applies to the copies of this step in the `validate-scripts` job of `mark-stable.yml` and `test-and-mark-stable.yml`.
