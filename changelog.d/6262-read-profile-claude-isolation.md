<!-- changelog: fixed -->
- **Read-profile Claude calls no longer expose runner credentials to model tools.** Read-only roles now run against a credential-free source snapshot in a network-isolated container, with the OAuth token kept in a host relay.

When Docker or required isolation support is unavailable, these calls fall back to the existing codex/OpenCode path instead of running on the host. Snapshot size limits default to 20,000 files and 256 MiB; read-profile tools cannot reach GitHub directly from the container.

Read-profile Git history is omitted when it contains filtered paths; other sessions' transcripts are not mounted. Standard extensionless SSH private keys are excluded even outside `.ssh`, and files such as `client.pem.txt` are excluded along with any Git history containing them.

What this means for operators: read-profile calls on runners without Docker continue through the existing fallback, with an `AI_ENGINE_FALLBACK` reason in the job log.
