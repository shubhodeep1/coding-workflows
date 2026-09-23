<!-- changelog: fixed -->
- **Release gates now ignore script paths found only in full-line comments and wait for authoritative review completion before verifying editor output.**

Both stable-release workflows now use the canonical workflow-reference checker instead of maintaining duplicate raw-text scanners. Reviewer-majority log lines remain progress telemetry and no longer allow the E2E release gate to proceed before the review workflow and editor have completed.
