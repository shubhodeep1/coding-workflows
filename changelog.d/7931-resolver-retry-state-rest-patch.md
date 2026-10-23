<!-- changelog: fixed -->
- **A token without `read:org` no longer fails the conflict resolver's retry-state write.** The resolver now writes the `AUTOFIX_RESOLVER_RETRY_STATE_V1` PR body block with a REST call instead of `gh pr edit`.

`gh pr edit` goes through GraphQL and asks for the `read:org` scope, so in a consumer run (tele-funtoken-msg-scoring#3933) the write failed five times in a row and the retry state was never recorded. `scripts/review_conflict_resolve.sh` now sends `PATCH repos/<repo>/pulls/<n>` with the body read from a file. The call keeps the same retries, and a failure still prints the same warning and skips the escalation side effects. Refs #7912.
