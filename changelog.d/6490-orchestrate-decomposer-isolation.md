<!-- changelog: security -->
- **The orchestrator decomposer now runs Claude and Codex in a credential-free sandbox.** Untrusted project descriptions can no longer prompt a host-side model process to read runner credentials.

Both engines use a read-only source snapshot and a host-side provider relay. An unavailable Claude attempt falls back to isolated Codex; sandbox setup failures retry and then fail closed without running a model on the host.
