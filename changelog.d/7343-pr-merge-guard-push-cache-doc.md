<!-- changelog: changed -->
- **CLAUDE.md §21.D now states that the merged-PR guard never answers a push from its cache.** Refs #6831.

§21.D (API-call budget) used to say cached data may satisfy an allow, which read as if a `git push` could pass on a PR state up to 300 seconds old. Both `pr_merge_status_guard.py` copies already refuse that: a `git push` or an MCP push always takes PR state from a live lookup, reuses only a snapshot from a live lookup in the same hook run, and follows §21.C when that lookup fails. The section now says so, and only a bare `git commit` may still be allowed from the cache. The guard itself is unchanged. This finishes the operator step recorded by #7297, so its `PR_MERGE_GUARD_DOC_SYNC_UNSET_OPERATOR_STEP` placeholder note is removed before it was ever released.

What this means for consumer repos: the next `@stable` sync brings the corrected §21.D text. Nothing to configure.
