<!-- changelog: fixed -->
- **Workflow failure heal now checks run provenance before reading job logs.** Phase, review and release reports must reference runs in the claimed repository with the expected workflow and failure state; phase and review reports also require an issue or PR link. Unverified reports are skipped without fetching logs.
