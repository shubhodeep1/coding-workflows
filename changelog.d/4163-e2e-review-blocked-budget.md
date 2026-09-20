<!-- changelog: fixed -->
- **The release E2E gate now reserves enough time to exercise review-blocked recovery and attributes the result to its own poller run.** Unsafe timeout combinations fail before test issues are created, while Phase 6 snapshots, registers, and pins one workflow-specific dispatch so concurrent pollers cannot replace its status or logs.
