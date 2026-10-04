### Security

- **Check-failure triage diagnosis now runs without checkout credentials or network access.** A read-only container examines a screened PR-head snapshot, and a separate trusted job rechecks the live PR before publishing a bounded, redacted diagnosis with the existing lineage and deduplication guards.
