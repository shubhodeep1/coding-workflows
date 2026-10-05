<!-- changelog: security -->
- **Single-issue security passes no longer permit merges without a completed audit of the current PR head.** Audit dispatch failures hold the merge, exhausted heads receive bounded additional retries before remaining held, and security-mode judge merges re-check the audited commit. Completed findings remain valid for the same head if a later attempt fails.
