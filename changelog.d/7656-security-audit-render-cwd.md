<!-- changelog: fixed -->
- **The project security pass renders its prompt again when it audits a clean worktree.** Since v1.33.0 every pass failed before the model ran, with `security-audit: phase=prompt-render ... error=prompt renderer exited nonzero`.

The security pass runs `scripts/security_audit.sh` inside `${RUNTIME_DIR}/security-pass-audit-wt-<tracking issue>`. From there the renderer could not find `prompts/references/output-contract.txt`: the orchestrate poller does not stage that file, and the renderer's only fallback was relative to the working directory. Both renderer calls now run from `SECURITY_AUDIT_SUPPORT_DIR`, so they resolve against the trusted support root and never against the audited branch's files. Projects that logged `SECURITY_PASS_FAILED reason=engine_unavailable` on every poll tick, such as tele-funtoken-msg-scoring tracking #3928, run their audit again after the next `@stable` promotion.

What this means for operators: there is nothing to change in consumer repos. Once `stable` carries this fix, the pending security pass runs on the next poll.
