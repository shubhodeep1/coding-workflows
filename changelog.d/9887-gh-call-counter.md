<!-- changelog: added -->
- **Each job's own `gh` calls are now counted next to the `GH_PAT` budget.** Every job that prints `GH_PAT_BUDGET`, plus eight release-gate jobs, now ends its job-end line with `calls_in_job=<n> paginated_in_job=<n> calls_by_token=pat:<n>,github_token:<n>,other:<n>`, and the daily PAT report ranks jobs by that count.

The `GH_PAT` (5,000 core calls per hour) runs out every hour, but `used_in_job` is the change in the shared remaining quota, so it also counts every job running at the same time and cannot rank the heavy users. A new `Install gh call counter` step sources the job's trusted `scripts/gh_helpers.sh` copy and runs `gh_call_counter_install`. It puts a small `gh` shim (`scripts/gh_call_counter/gh`) first on `PATH` for the rest of the job. The shim logs one line per call (the subcommand, a `--paginate` bit and the token kind, decided by variable name; never an argument or a value) and then runs the next `gh`, which may be the comment-signing shim, so signing is never bypassed. `gh_pat_budget end` and the inline end steps read the count before their own `rate_limit` read. `scripts/collect_workflow_logs.py` parses old and new lines and adds a "Top consumers by `calls_in_job`" table. The release-gate jobs `e2e-smoke-test`, `e2e-alt-model-test`, `validate-standalone-test`, `workflow-log-analysis-test`, `orphan-workflows-test`, `orchestrate-decompose-test`, `clarify-rejects-unsolvable-test` and `validate` in `test-and-mark-stable.yml` gain start and end budget lines without a `rate_limit` read, so their quota keys stay `unknown` and the gate makes no extra request. Existing keys keep their order and meaning, and no GitHub request is added.

| The numbers that matter | Value |
| --- | --- |
| Jobs instrumented | 22 (14 existing `GH_PAT_BUDGET` jobs, 8 release-gate jobs) |
| New end-line keys | 3, appended after `used_in_job=` |
| GitHub requests added by the counter | 0 |

What this means for consumer repos: after the next `@stable` sync, job-end `GH_PAT_BUDGET` lines carry the three new keys. Set the repository variable `GH_CALL_COUNTER_ENABLED=false` to turn the counter off; the keys then read `unknown`.

Refs #5504. Refs #9888.
