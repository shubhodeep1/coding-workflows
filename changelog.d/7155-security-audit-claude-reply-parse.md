<!-- changelog: fixed -->
- **The security audit keeps Claude's findings when the reply has text around the JSON, and logs why a reply was rejected.** Before, a single sentence before the findings array sent the whole audit to codex as `malformed_output`, and nothing showed what Claude had written.

`scripts/security_audit.sh` still accepts a reply that is one JSON array or one ```` ```json ```` fence around it. Otherwise it now uses the last top-level JSON array whose items are all objects with a `finding_id`, and logs `SECURITY_AUDIT_CLAUDE_EXTRACTED`. An empty array counts only when nothing but a closing fence follows it, so a `[]` in prose or a cut-off reply never passes as a clean audit, and the existing post-filter checks still apply to every finding. Every Claude fallback now prints `SECURITY_AUDIT_CLAUDE_FALLBACK reason=` in the job log; the orchestrator's security pass writes the audit's stderr to a file, so the `AI_ENGINE_FALLBACK` line never showed there. A rejected reply also prints `SECURITY_AUDIT_CLAUDE_REJECTED` with its first and last 300 characters, with control characters removed and credentials redacted.

| The numbers that matter | Value |
| --- | --- |
| Poller security passes that fell back on `malformed_output` | 2 of the last 3 before this change |
| Reply excerpt in the job log | first and last 300 characters |
| Reply kept at `SECURITY_AUDIT_CLAUDE_REJECTED_OUT` | first 64 KiB |

What this means for operators: fewer security passes fall back to codex for a formatting slip, and the next rejected reply shows its cause in the poll job log. The new `SECURITY_AUDIT_CLAUDE_REJECTED_OUT` variable is optional and unset by default; the orchestrator's security pass sets it to a file in its runtime directory.
