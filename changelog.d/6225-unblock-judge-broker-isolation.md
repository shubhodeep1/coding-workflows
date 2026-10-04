<!-- changelog: fixed -->
- **Unblock judge Codex no longer receives the OpenRouter key.** Its read-only model call runs in a network-isolated container through a host-side broker; verdicts containing literal or encoded credentials are rejected instead of posted. If isolation fails, the judge waits for another run rather than executing Codex on the host.
