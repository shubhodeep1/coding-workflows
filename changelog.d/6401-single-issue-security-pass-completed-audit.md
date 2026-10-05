<!-- changelog: security -->
- **Exhausted single-issue security passes no longer permit merges without a completed audit of the current PR head.** Unaudited heads receive bounded retry attempts before remaining held, and security-mode judge merges re-check the audited commit.
