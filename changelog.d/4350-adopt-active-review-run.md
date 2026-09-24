<!-- changelog: fixed -->
- **Stable-release editor recovery now adopts review work already queued for the smoke PR.** Phase 4b dispatches only when no eligible active run exists, then pins and polls one exact run ID so serialized review queue time is not multiplied by duplicate work.
