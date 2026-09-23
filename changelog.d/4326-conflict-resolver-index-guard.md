<!-- changelog: fixed -->

- **Conflict resolution now fails closed when Git still reports unmerged paths.** Review/autofix summaries also identify unsuccessful resolver execution as `conflict_resolver_failed`.

Trusted runner staging now surfaces path-specific failures and checks the Git index before creating an `[ai-merge-resolve]` commit. This prevents marker-free modify/delete conflicts or failed staging operations from being reported as clean no-op reviews. Existing resolver isolation and retry behavior remain unchanged.

What this means for operators: resolver failures remain visible and actionable instead of being misclassified as successful clean reviews.
