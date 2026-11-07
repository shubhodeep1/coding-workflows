<!-- changelog: added -->
- **Stall and timeout heal issues can now show a run-time trend.** Behind `WORKFLOW_HEAL_TREND_ENABLED` (off by default) the heal intake adds a `## Run-time trend` section to heal issues whose failure is a stall or a timeout: per UTC day, the number, median and p90 duration of the last 7 days of successful runs of the failing workflow and of the workflows of its linked runs. One line, `- regression=<true|false> first_slow_day=<YYYY-MM-DD|none> history=<complete|truncated>`, says whether the latest day is markedly slower than before and on which day the slowdown started, so a heal plan can look for the change that made runs slower instead of only raising a timeout.

A table whose history could not be read completely is marked `history=truncated` and never reports a regression. Other failure shapes get no section and no extra API call, and with the switch off the heal issue is unchanged.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_TREND_ENABLED=false`) |
| Regression ratio | `1.5` (`WORKFLOW_HEAL_TREND_REGRESSION_RATIO`) |
| Workflows per report | at most 3 |
| Extra GitHub API calls | at most 6 per stall / timeout report (2 pages per workflow) |
| Log line | `WORKFLOW_HEAL trend outcome=<ok|partial|skipped> ...` |

What this means for operators: nothing changes until the switch is on; then stall and timeout heal issues carry the trend, and the trend never blocks an issue from being filed.
