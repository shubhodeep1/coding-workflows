<!-- changelog: added -->
- **Merged workflow heals are now checked after the merge.** Behind `WORKFLOW_HEAL_VERIFY_ENABLED` (off by default) a new `heal-verify` job on the heal intake's hourly schedule looks at heal issues closed in the last 7 days by a merged pull request. It waits for the first decisive run of the failed workflow on the target branch at or after the merge commit. A pass leaves a `held` marker; a failure at the same step reopens the heal with a `## Fix did not hold` comment, or links the newer heal when the intake already filed one in the same lineage; a failure at another step is recorded as inconclusive. A heal whose fix fails a second time also gets `ai:workflow-heal-escalated` and a CRITICAL alert.

Only this repository's release, CI and scheduled-check heals are verified; consumer and pull-request heals are skipped. Markers count only when the pipeline account wrote them, fork and pull-request runs are ignored, and a failed GitHub call never writes anything: the next hourly run retries.

| The numbers that matter | Value |
| --- | --- |
| Default | off (`WORKFLOW_HEAL_VERIFY_ENABLED=false`) |
| Heals checked per hour | 3 (`WORKFLOW_HEAL_VERIFY_MAX_PER_TICK`, 1-10) |
| Window | heals closed in the last 7 days |
| GitHub API calls | 1 list query per hour, then at most 3 reads per heal |
| Log line | `WORKFLOW_HEAL_VERIFY outcome=<held|recurred|recurred_lineage|inconclusive|pending|skip|error> ...` |

What this means for operators: nothing changes until the switch is on; then a heal whose fix did not hold comes back on its own instead of waiting for the same failure to be reported again.
