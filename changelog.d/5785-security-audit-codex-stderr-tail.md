<!-- changelog: changed -->
- **A failed security audit now shows why Codex failed.** When Codex exits nonzero, `scripts/security_audit.sh` prints a sanitized tail of Codex stderr, and the failure line names the provider failure class.

Until now a `security-audit.yml` run that failed at **Run security audit** logged only `phase=codex-execution … error=Codex\ exited\ nonzero`. That is all three 2026-09-30 runs showed during the OpenRouter credit outage (HTTP 402). The job log now carries a block framed by `security-audit: codex-stderr-tail begin lines=<n> omitted_lines=<m>` and `security-audit: codex-stderr-tail end`, with one `security-audit: codex-stderr-tail line=<value>` per stderr line. Every line is masked for `sk-` keys, long hex and base64 runs (standard or URL-safe, padded or not), and the values of secret-named env vars (short values where they stand as a whole word), then passed through the existing log sanitizer. The `security-audit: captured_path_error=` line printed from Codex stderr just before the tail gets the same masks. Lines that echo the audit prompt are dropped. The failure line now ends with `provider=<402|401|429|5xx|unknown>`, so a stage session can tell a provider outage from a code failure without reading the log by hand. The orchestrator's project security pass runs the same script and gets the same output.

| The numbers that matter | Value |
| --- | --- |
| Tail size | at most the last 40 non-blank lines and 4096 bytes of sanitized text |
| Stderr read | at most the last 64 KiB; the line the 64 KiB boundary cuts is dropped |
| Provider classes | `402`, `401`, `429`, `5xx`, `unknown` (the newest matching line wins) |
| Source runs | 36748018397, 36749008453, 36754588874 (2026-09-30) |

What this means for operators: the next audit that fails on a provider outage says so in its log, for example `provider=402` for exhausted credits. Other failure phases and successful runs print exactly what they did before.

### For contributors

The new helpers are `security_audit_mask_stderr_line`, `security_audit_classify_codex_provider`, and `security_audit_emit_codex_stderr_tail`. `security_audit_emit_failure` takes an optional fourth `provider` argument, and only the `codex-execution` branch passes it. `tests/test_security_audit_workflow_contract.py` covers the 402 tail with masked keys, the 40-line and 4 KiB caps, the 64 KiB cut (including a cut prompt line), each provider class, prompt-echo filtering, and a secret on a Codex path-error line. `security_audit_emit_path_diagnostic` takes an optional second `mask` argument, and only the `codex-execution` branch passes it. The `provider=` field is meant for #5773's provider-outage handling.
