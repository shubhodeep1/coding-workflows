<!-- changelog: security -->
- Integration-conflict judge instructions require retaining shared protected-file lines.

The poller rejects a protected-file resolution that removes any occurrence present on both merge sides, including lines both sides added independently. The judge prompt now states that requirement. Rejected resolutions are not pushed; no automated deletion override exists.

What this means for operators: resolve any protected-file merge that genuinely needs to remove shared lines manually.
