<!-- changelog: security -->
- **Workflow-heal evidence no longer goes into a PR-readable Actions cache.**

Clarify, plan and implement now collect logs and artifacts, including consumer-repository evidence, afresh in each trusted stage instead of saving them for fork pull-request runs to read. Each stage uses about 20 REST calls, rather than about 5 for a later stage with a cache hit. An eligible heal stage also lists the first page of legacy `heal-evidence-*` caches and attempts to delete matching entries. Cleanup failures do not block evidence collection, and entries not yet deleted remain readable until deleted or expired.

| The numbers that matter | Value |
| --- | --- |
| Evidence collection | About 20 REST calls per stage |
| Legacy cache cleanup | One list call for up to 100 entries, then one delete call per matching entry |
| Automatic cache eviction | 7 days after the last access |

What this means for operators: new heal evidence is no longer shared through Actions caches. Existing cache entries need a successful cleanup pass or expiration before they stop being readable by pull-request runs.
