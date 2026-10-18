<!-- changelog: fixed -->
- **A security audit that fails while rendering its prompt now logs the renderer's own error.** Before, the run log showed only `error=prompt renderer exited nonzero`.

`scripts/security_audit.sh` kept renderer stderr only when a line looked like a missing path, so every other render failure left nothing to act on. Project security passes for tele-funtoken-msg-scoring#3928 failed this way on every poll tick from 2026-10-16. A render failure in the main audit prompt or the money-lens template now prints the renderer's error lines between `security-audit: prompt-render-stderr-tail begin` and `end`, one `security-audit: prompt-render-stderr:` line each. Only error-shaped lines are kept, with the same secret masking, prompt-echo filter and size cap the Codex stderr tail already uses.

| The numbers that matter | Value |
| --- | --- |
| Lines kept per failure | at most 40 |
| Bytes kept per failure | at most 4,096 |
| Failure sites covered | 2 (audit prompt, money-lens template) |

What this means for consumer repos: when a project's security pass reports `SECURITY_PASS_FAILED reason=engine_unavailable` after a prompt-render failure, the run log now names the cause. Codex failure output is unchanged.
