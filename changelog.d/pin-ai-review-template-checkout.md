<!-- changelog: security -->
- **The `ai-review.yml` consumer wrapper now pins `actions/checkout` to a full commit SHA.** Its three scheduled support jobs used the movable `actions/checkout@v5` tag while holding `GH_PAT`, `OPENROUTER_API_KEY` and `TG_BOT_SECRET`.

`workflow-templates/ai-review.yml` now uses `actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09  # v5.1.0` in the non-retryable backfill, security-hold sweep and provider-outage resume jobs. That SHA is the commit both the `v5` and `v5.1.0` tags point to today, so the checkout code that runs does not change. The sync renderer (`scripts/workflow_wrapper_refs.py`) already leaves action refs as written, so consumers get the pinned lines unchanged. A new guard test fails if any template adds a third-party `uses:` that is not pinned to a 40-hex SHA.

| The numbers that matter | Value |
| --- | --- |
| Lines changed in `workflow-templates/ai-review.yml` | 83, 121, 161 |
| Unpinned third-party `uses:` left in `workflow-templates/` | 0 |
| Consumer finding cleared | fun-token-multi-chain#640, `review-mutable-checkout-tag-with-secrets` (2026-10-25 audit) |

What this means for consumer repos: after the next `@stable` sync, `ai-review.yml` passes the R1 rule of a consumer's own workflow-ref pinning gate (for example fun-token-multi-chain's `Workflow Ref Pinning Gate`), so that gate can be made a required check on `main` without blocking every PR.
