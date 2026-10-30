<!-- changelog: fixed -->
- **The orchestrator no longer merges a ready PR when it could not list that PR's siblings.** A failed or malformed `gh pr list` used to look like "no open sibling PRs", so the pre-merge conflict probe passed and the PR was squash-merged even when a sibling would conflict.

`_merge_probe_refresh` in `scripts/orchestrate_poll_process.sh` now records whether the sibling lookup for each integration branch worked (`known`) or not (`unknown`: the command failed, or returned empty or non-array output). `probe_sibling_merge_conflicts` returns a new exit code 2 for `unknown` and logs that it cannot probe the PR this tick. The ready-to-merge loop then skips that PR until the next tick. It does not count the skip toward `MAX_MERGE_DEFERRALS`, sends no alert and writes no state. Exit codes 0 (no conflict) and 1 (conflict) and the other fail-open cases (missing inputs, no git, no `merge-tree`, candidate ref missing) are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Sibling lookups per integration branch per tick | 1 (unchanged) |
| New probe exit code | 2 (lookup unavailable) |
| Deferrals counted for an unavailable lookup | 0 |

What this means for operators: during a GitHub API outage, ready PRs wait instead of merging unchecked, and they merge on the first tick after the lookup works again. `MERGE_PROBE_SIBLING_LOOKUP_FAIL_CLOSED_ENABLED=false` restores the old behaviour; any value other than unset, empty, `true` or `false` also restores it, with one warning. The variable is read only from the script's environment: `orchestrate_poll.yml` does not forward it, so a repository variable alone does not reach it.
