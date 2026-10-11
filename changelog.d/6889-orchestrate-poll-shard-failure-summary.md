<!-- changelog: changed -->
- **A failed `orchestrate-poll` CI shard now names its failing tests at the end of the job log.** Follow-up to the inconclusive check-failure triage of PR #6881 (#6889).

Each shard's log is printed inside a collapsed `::group::`, and the step used to end with only `::error::N orchestrate-poll shard(s) failed.`. The log tail that check-failure triage reads therefore never named the failing test, and both triage generations for #6881 came back inconclusive. Before that final line, the `Orchestrate poll process unit tests` step in `.github/workflows/ci.yml` now prints one annotation per failed shard, outside every group:

- `::error::orchestrate-poll shard <n> exit <rc> failed tests: <names>` lists up to 20 names from the shard's `  FAIL  <name>:` lines, followed by `(+K more)` if there are more.
- A shard that exited non-zero without any `FAIL` line reports `no FAIL lines (process failure)`.
- A shard that produced no log reports `exit none failed tests: no log (tests did not run)`.

Only names made of letters, digits and `_` are printed, so text inside a failure message cannot add workflow commands to the annotations. Pass/fail behaviour is unchanged: the same shards fail the step, and the existing `failed (exit <rc>)`, `produced no log` and tally lines are kept byte for byte. The release gates' copies of this step (`mark-stable.yml`, `test-and-mark-stable.yml`) are unchanged.
