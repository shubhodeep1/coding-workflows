<!-- changelog: fixed -->
- **The orchestrator's project security pass works again in consumer repositories.** Since #6667 it had failed on every poll tick with `engine_unavailable`, so the project's final pull request was never moved out of draft and the project could not finish.

Since #6667 the audit runs in a fresh worktree of the integration head (`security-pass-audit-wt-<N>`). That worktree has no `.codex-workflow-src/`, so `render_prompt.sh` could no longer find `prompts/references/output-contract.txt`, which `prompts/mode-security-audit.txt` needs for `{{REFERENCE_OUTPUT_CONTRACT}}`. The "Stage workflow support files" step never staged that file. Every tick logged `security-audit: phase=prompt-render ... error=prompt renderer exited nonzero` and then `SECURITY_PASS_FAILED reason=engine_unavailable`. Seen on tele-funtoken-msg-scoring#3928 (final PR #3933, first in run 38837807028) and #4038 (final PR #4045).

The fix:

- `orchestrate_poll.yml` now stages `prompts/references/output-contract.txt` and `prompts/contracts/mode-security-audit.yml` from the verified support checkout, with the same `.codex-workflow-src-main` fallback as the other prompt assets. Before #6667 the renderer found the contract through `.codex-workflow-src`; staging it keeps the audit's output contract applied.
- The poller now requires `prompts/references/output-contract.txt` and `prompts/contracts/mode-security-audit.yml` before the audit starts. A missing file fails closed with `Required security-pass asset <path> is unavailable.` instead of a generic render failure.
- `scripts/security_audit.sh` now also logs the renderer's `... not found: <path>` errors as `security-audit: captured_path_error=`.

What this means for operators: nothing to do. Consumers stage support files from `stable`, so stuck projects resume on their own once this change reaches a `stable` release; `engine_unavailable` is retried every tick.

### For contributors

`tests/test_security_audit_workflow_contract.py` renders `prompts/mode-security-audit.txt` from an empty working directory using only the files the poll workflow's staging loop names, and checks it fails without `output-contract.txt`.
