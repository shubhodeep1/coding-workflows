<!-- changelog: security -->
- **Orchestrator judge isolation latches remain in place when label reads fail.** An escalated judge resumes only after a live issue read confirms `ai:needs-human` was removed; a failed or malformed read defers the judge to the next poll tick.
