<!-- changelog: security -->
- **Claude-selected conflict resolution now fails closed when isolation is unavailable.** Unsupported conflict paths and sandbox setup failures can no longer route untrusted PR content to host OpenCode with runner credentials; Claude unavailability retries OpenCode in a fresh credential-free sandbox.
