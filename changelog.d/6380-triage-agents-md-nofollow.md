<!-- changelog: security -->
- **Check-failure triage no longer follows PR-head agent-file symlinks when building diagnosis prompts.**

Agent files are now read as bounded regular files by a credential-free process. Symlinks and other non-regular files are omitted, and oversized files are truncated, preventing a PR from pulling runner credentials into the model prompt.
