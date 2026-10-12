<!-- changelog: changed -->
- **Stable releases publish by default again. Set `STABLE_RELEASE_ENABLED=false` to hold them.**

The publishing switch added for the stable release gate was off unless the repository variable `STABLE_RELEASE_ENABLED` read `true`. The variable was never set, so every release after v1.31.0 would have passed `mark-stable.yml` or `test-and-mark-stable.yml` and then skipped the tag, the GitHub Release and the consumer dispatch. Consumer repos would have stayed on v1.31.0. The switch now publishes when the variable is unset, empty or `true`. `false` holds a release, and any other value also holds it and logs a warning, so a typo never publishes. Stable tagging was not meant to sit behind an off switch (#7029).

| The numbers that matter | Value |
| --- | --- |
| `STABLE_RELEASE_ENABLED` default | `true` (was `false`) |
| Values that hold a release | `false`, or anything other than unset, empty or `true` |
| Last published stable release | v1.31.0, 2026-10-09 |

What this means for operators: no variable to set for releases to reach consumer repos. To hold releases, set `STABLE_RELEASE_ENABLED=false`, and also set `AUTO_RELEASE_STABLE_ENABLED=false` so the 6-hourly auto-release does not re-run the gate on the same commit.
