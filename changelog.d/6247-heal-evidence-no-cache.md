<!-- changelog: security -->
- **Workflow-heal evidence no longer goes into a PR-readable Actions cache.** Logs and artifacts, including those from consumer repositories, are collected afresh in each trusted stage instead of being saved where fork pull-request runs could read them.

Each stage now uses about 20 REST calls instead of about 5 in later stages. On the next eligible heal stage, a best-effort cleanup deletes legacy `heal-evidence-*` cache entries; entries readable before that cleanup remain exposed until deleted or expired.
