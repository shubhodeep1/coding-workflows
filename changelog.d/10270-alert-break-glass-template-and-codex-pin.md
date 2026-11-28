<!-- changelog: fixed -->
- **The alert break-glass review prompt has its `prompts/_templates/` twin, and both alert workflows fall back to Codex `v0.171.0`.** Refs #9394 (issue #10270).

  - `prompts/_templates/mode-alert-break-glass-review.txt` is a byte-for-byte copy of `prompts/mode-alert-break-glass-review.txt`, so the prompt template parity tests pass again.
  - `.github/workflows/alert-triage.yml` and `.github/workflows/alert-self-fix.yml` now use `v0.171.0` as the `vars.CODEX_VERSION` fallback, like every other workflow; `tests/test_codex_agent_isolation_contract.py` pins it for both.
