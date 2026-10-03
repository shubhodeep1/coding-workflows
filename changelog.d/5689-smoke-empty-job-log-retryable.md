<!-- changelog: fixed -->
- **The release gate now retries when a review job's log is empty or whitespace-only.** A log still uploading no longer makes the E2E smoke test reject a genuine review run.

In `test-and-mark-stable.yml`, Phase 4 and Phase 4b read a review run's `codex-agent` log to confirm which PR head it checked out. An empty or whitespace-only response now receives the existing retryable result, so those phases can poll again within their deadlines. A non-empty log without the checkout line still rejects the run, and the first matching checkout line remains authoritative. This changes no GitHub API calls or retry deadlines.

What this means for operators: a delayed job-log upload no longer fails the smoke gate immediately; a log that never becomes verifiable still fails when the existing polling deadline expires.
