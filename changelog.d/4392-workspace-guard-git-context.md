<!-- changelog: fixed -->
- Restore validated, read-only Git context only for the post-agent workspace guard when a review workspace has no `.git`. Ignore classification now quarantines ignored edits instead of failing after the editor completes; model and validator processes still receive no Git context.
