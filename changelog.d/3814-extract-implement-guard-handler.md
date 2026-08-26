<!-- changelog: fixed -->
- **Implementation guard-rejection handling now stays below the GitHub Actions step-size limit.** The destructive-delete and scope-block rejection shell has been moved into `scripts/implement_handle_guard_block.sh`, while `implement.yml` keeps the same trigger conditions and environment bindings.

The helper is staged into the runtime directory before fetched-support cleanup, so late guard failures in source and consumer repositories can still latch `ai:destructive-blocked` or `ai:scope-blocked`, post the same issue comments, and send the same direct Telegram fallback alerts after repository support files are removed.

Focused recovery tests now inspect and execute the helper directly, including the cleanup-survival path and the reduced workflow step body size.
