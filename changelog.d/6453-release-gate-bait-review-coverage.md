<!-- changelog: fixed -->
- **Stable-release smoke tests no longer count a pre-bait review as proof that the editor saw the bait.** When a review was already running at bait injection, the release gate verifies its checked-out commit from its job log before accepting its result. Unverified or pre-bait checkouts keep the gate waiting for the bait-head review within the existing budget.
