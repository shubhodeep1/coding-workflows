<!-- changelog: fixed -->
- **Check-failure triage no longer loads PR-authored agent instructions into its diagnosis sandbox.**

The PR-head snapshot omits agent instruction files at every depth before Codex or Claude starts. Root agent files remain available to the diagnosis only as explicitly untrusted prompt data. Clarify and clarify-respond keep their existing snapshot behavior.
