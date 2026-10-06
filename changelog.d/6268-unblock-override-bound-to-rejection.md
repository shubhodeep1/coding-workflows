<!-- changelog: security -->
- **Unblock guard overrides are bound to the actual rejection.** The unblock judge now accepts scope and bulk-deletion overrides only for the exact paths rejected by the latest trusted guard run; extra, missing, stale, or incomplete paths cannot clear the block.

Guard comments record their rejected paths and run ID in a bounded marker. Scope-lock and non-bulk destructive rejections cannot use this override. Existing consumer blocks without a marker remain blocked from override until a new guarded run records one; other judge verdicts remain available.
