<!-- changelog: security -->
- **Read-profile Claude runs no longer see the runner's account tokens.** They run against a filtered repository snapshot in a no-network container, authenticated through a host-side relay. Refs #3576.

The read-only engine roles and calls narrowed by `AI_ENGINE_READ_ONLY=true` share this isolation path. If the container, policy, or snapshot cannot start, the call falls back to its existing non-Claude engine instead of running Claude on the host. Write-profile roles are unchanged and still need a separate credential-isolation follow-up.

What this means for operators: a read-role prompt cannot open the host pool's token files, and failed isolation is visible as `CLAUDE_READ_ISOLATION outcome=unavailable`.
