<!-- changelog: fixed -->
- **Stable releases now recover safely from ambiguous Git tag push failures.**

Release operators no longer lose an otherwise valid release when GitHub accepts a tag push but times out before confirming it. `.github/workflows/mark-stable.yml` and `.github/workflows/test-and-mark-stable.yml` now retry tag publication with exponential backoff and inspect the exact remote tag after each failed push. A matching remote object confirms that publication succeeded despite the failed response. A conflicting immutable version tag still fails without force, while only the existing `stable` and major pointers retain force-update behavior.

| The numbers that matter | Value |
| --- | --- |
| Maximum publication attempts per tag | 3 |
| Retry delays | 2 seconds, then 4 seconds |
| Remotely verified tags | Version, `stable`, and major-version tags |

What this means for release operators: transient, ambiguous GitHub responses can recover automatically, while genuine immutable-tag conflicts and unverifiable remote state continue to block the release.
