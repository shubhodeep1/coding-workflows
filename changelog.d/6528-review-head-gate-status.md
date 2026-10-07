<!-- changelog: security -->
- **Auto-merge no longer remains enrolled across reviewed-head changes without a new review.** A synchronize event withdraws stale enrollment, and review and security approval publish an `ai-review/head-gate` status on the evaluated commit. Requiring the status in default-branch protection also blocks pushes that do not trigger a synchronize event. Refs #3576.
