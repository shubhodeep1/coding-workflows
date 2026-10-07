<!-- changelog: security -->
- Integration-conflict judge instructions require retaining shared protected-file lines.

The poller rejects a protected-file resolution that removes any occurrence present on both merge sides, including lines both sides added independently. The judge prompt now states that requirement. Rejected resolutions are not pushed; one-sided delete/modify conflicts can still resolve to deletion, but no automated override permits removing shared lines.

What this means for operators: resolve any protected-file merge that genuinely needs to remove shared lines manually.
