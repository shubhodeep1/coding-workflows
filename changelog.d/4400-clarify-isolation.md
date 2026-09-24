<!-- changelog: fixed -->
- **Clarification agents no longer run with access to runner credentials.** The Codex step now executes inside a read-only container with a sanitized source snapshot, no Git metadata or host tokens, and no external network. A narrow host-side model broker keeps the provider key outside the container. Issue fetching, memory, comments, retries, and clarification output parsing remain on the runner.

### For contributors

The clarify workflow stages `clarify_isolated_run.sh`, `clarify_openrouter_broker.py`, and the pinned sandbox Dockerfile with its support files. Missing Docker, image build failures, or broker errors fail the Codex step instead of retrying with host access.
