<!-- changelog: fixed -->
- **Read-only Claude runs can no longer use Git output options to overwrite trusted support scripts.** A changed support checkout stops the audit, heal intake or poller instead of allowing privileged follow-up steps.

Read-profile Bash commands now pass an exact-subcommand and dangerous-option guard. Trusted support is locked and hashed during read-profile runs; failure to lock or a mismatch returns exit 86 without falling back to codex. Independent checks before later workflow steps refuse to execute modified support.

### For contributors

`scripts/claude_engine.py` provides `guard-read-bash`, `support-lock`, `support-verify` and `support-unlock`. `AI_ENGINE_SUPPORT_LOCK` logs lock and verification results. A stopped self-hosted job may leave directories read-only; use the surviving manifest to restore original modes before reusing the workspace.
