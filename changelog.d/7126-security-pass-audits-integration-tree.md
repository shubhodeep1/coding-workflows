<!-- changelog: security -->
- **The orchestrator security pass audits the integration branch's own files, not the default branch's support scripts.** Refs #7126 (security-pass fix cycle 2).

Before the security pass runs, it replaces every `scripts/` and `prompts/` file in its checkout with the verified copy from the default branch, so the audit never runs code the audited branch supplied. The audit then read that same checkout. The model saw the default branch's `scripts/` and `prompts/` beside the integration branch's changelog fragments and diff. Two things followed: fixes made on the integration branch to those directories were reported again as still missing, and a defect the branch added there was never audited.

Now the pass checks the integration head out into a separate, clean, detached worktree under the runner's temporary directory and audits that tree. The audit engine, its prompt and its false-positive exclusion list still come from the verified support copy, by absolute path, so a branch cannot supply its own exclusion list to hide a finding. The worktree is removed after the audit, whether it passed or failed. If the worktree cannot be created, is not at the expected commit, or is not clean, the pass fails closed as `engine_unavailable` and retries on the next tick. The log line `SECURITY_PASS_AUDIT_TREE` records each step.

What this means for operators: nothing to configure. Projects whose security pass kept reporting findings already fixed in `scripts/` or `prompts/` get an accurate audit on their next pass.
