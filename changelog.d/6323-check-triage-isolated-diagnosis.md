<!-- changelog: security -->
- **Check-failure triage now diagnoses PR failures inside a credential-free, read-only container.**

The check-failure triage wrappers pass only their four declared secrets, and the PR checkout uses a read-only job token instead of `GH_PAT`. The diagnosis runs through the existing clarify isolation helper with trusted support, a network-disabled container, and a host-side model broker. Issue posting checks the triage fingerprint marker and caps bodies at 60,000 characters. If Docker or isolation support is unavailable, triage files a raw-context issue rather than running host Codex.

The Claude Anthropic relay also rejects incomplete POST bodies after a 60-second total read deadline, so one stalled client cannot block the single-threaded relay indefinitely.

What this means for consumer maintainers: the workflow requires Docker for model diagnosis; without it, the normal issue pipeline still receives the raw failure context.
