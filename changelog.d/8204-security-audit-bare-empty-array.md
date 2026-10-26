<!-- changelog: fixed -->
- **The security audit prompt now says that a clean audit's final message is exactly `[]`, with no summary after it.** A weekly Security Audit that found nothing failed because Claude replied `[]` and then a paragraph listing what it had checked.

Security Audit run 40333816067 (#8204) was rejected as `malformed_output`. `scripts/security_audit.sh` reads an empty array as a clean audit only when nothing but a closing fence follows it, so a reply such as "no findings yet, still checking" is never mistaken for a pass. That rule is unchanged. Under the default `AI_ENGINE_FALLBACK_POLICY=capacity`, `malformed_output` does not rerun on codex, so the audit failed. The "Output contract" in `prompts/mode-security-audit.txt` and its `prompts/_templates/` source now adds two rules: the final message is the array alone, with nothing before or after it apart from one optional `json` fence around it, and a clean audit replies exactly `[]` without explaining itself.

| The numbers that matter | Value |
| --- | --- |
| Rejected reply | `[]` followed by 1,221 characters of prose |
| Prompt files changed | 2 (rendered prompt and its template) |
| Extractor or fallback changes | none |

What this means for operators: a clean weekly or project-pass security audit should stop failing on a trailing summary. The parser stays strict, so a reply that still adds text after `[]` keeps failing, and the log's `SECURITY_AUDIT_CLAUDE_REJECTED` line still shows its start and end.
