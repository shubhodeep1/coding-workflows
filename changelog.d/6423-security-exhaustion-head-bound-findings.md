<!-- changelog: fixed -->
- **An exhausted single-issue security pass no longer merges past an unticketed high-severity finding.**

Branch audits now deduplicate findings only against open tickets on the same branch. The audit publishes a head-bound findings record alongside the trusted PR result; the review-blocked judge checks that record as well as open tickets. A closed ticket or a ticket on another branch cannot clear a blocking finding. Existing exhausted PRs without a findings record stay held until they receive another audit or a fix earns a new cycle.
