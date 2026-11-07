<!-- changelog: added -->
- **Heal issues can now group reports by root cause.** Behind `WORKFLOW_HEAL_CAUSE_GROUPING_ENABLED` (off by default) the heal intake gives each workflow-run or phase failure a cause key built from the workflow name, the failing step and the error class (stall, timeout, test failure, crash, API error or other). A new heal issue records that key in a sixth hidden header line. A later report whose error lines differ slightly (so its fingerprint differs) but whose cause matches an open heal issue filed in the last `WORKFLOW_HEAL_CAUSE_GROUPING_DAYS` days (default 3) is recorded as an occurrence on that issue, with its run link, its error signature and, when the evidence pack is on, its `## Evidence pack`, instead of opening a second heal issue for the same problem.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_CAUSE_GROUPING_ENABLED=false`) |
| Match window | `3` days (`WORKFLOW_HEAL_CAUSE_GROUPING_DAYS`, 1 to 30) |
| Extra GitHub API calls | none |
| Log line | `WORKFLOW_HEAL duplicate reason=cause_match ...` |

What this means for operators: nothing changes until the switch is on; then near-identical failures stop filing separate heal issues, and fingerprint, source and re-issue matches and the lineage cap still take precedence.
