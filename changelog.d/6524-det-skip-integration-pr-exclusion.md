<!-- changelog: fixed -->
- **Small orchestrator integration PRs no longer bypass the final-merge gates.** The deterministic review-skip path now refuses auto-merge and merge-authorization labels for integration branches, including when the configured branch pattern is invalid. An unavailable head ref also blocks merge authorization until the PR can be identified.
