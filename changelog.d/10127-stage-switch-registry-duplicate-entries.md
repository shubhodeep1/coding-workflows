<!-- changelog: fixed -->
- **Pull requests #9359 and #9654 no longer fail the stage switch registry test on a duplicate entry.**

PR #10128 listed 32 switches in `scripts/stage_switches.json` on behalf of 17 open pull requests. Two of those pull requests, #9359 and #9654, had by then added their own entries for the same switches, so the merged tree listed 12 names twice and `test_registry_entries_are_well_formed` failed their final CI. This change removes those 12 entries from `main`; the pull requests' own entries, which carry the full stage text and `on_values`, remain the only copy. The other 20 entries stay, and nothing changes at run time.

| The numbers that matter | Value |
| --- | --- |
| Entries removed from `scripts/stage_switches.json` | 12 (207 to 195) |
| Pull requests that listed a name twice on the merged tree | 2 of 52 open (#9359: 1 name, #9654: 11 names) |
| Pull requests with a duplicate or a missing switch after this change | 0 of 52 |
| First failure | `CI [pr:9359]` run 45695791182, `assert 208 == 207` |

What this means for operators: a final CI failure on `test_registry_entries_are_well_formed` in #9359 or #9654 should stop on their next final CI run. If another open pull request adds its own entry for a switch that `main` already lists for it, the same failure appears until one of the two copies is removed; #10127 ends this by dropping the hand-kept list.
