<!-- changelog: security -->
- **Unresolved security findings stay visible.** The unblock judge keeps `ai:security` issues open when its verdicts are exhausted, with a terminal label and a CRITICAL alert. On a safe standalone reissue, it carries the finding marker and security label to the replacement; otherwise the original stays open until a linked fix is verified merged.
