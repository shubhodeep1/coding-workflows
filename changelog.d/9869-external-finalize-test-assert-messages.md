<!-- changelog: changed -->
- When the branch-deleted external-finalize security-pass test fails, its assertions now print the poller state, the fetch and PR-read counts, and the matching `EXTERNAL_FINALIZE_DELIVERY` / security-pass log lines. Behaviour is unchanged. Refs #9869
