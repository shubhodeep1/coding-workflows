<!-- changelog: fixed -->
- **The review support-SHA contract test now accounts for the deterministic-skip freshness check.**

The test expects the third verified gate-SHA export used to verify the freshness-helper checkout, alongside the fingerprint-cap and codex-agent jobs. The `review_autofix.yml` size-guard failure remains open: the workflow is 481,287 bytes against the 480,000-byte guard. A separate change must move large inline `run:` bodies into `scripts/review_autofix_step_*.sh` (agents.md, "Workflow file size limit").
