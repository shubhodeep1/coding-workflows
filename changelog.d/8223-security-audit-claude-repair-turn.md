<!-- changelog: fixed -->
- **A Claude security audit that adds text around its JSON gets one chance to reply again.** When the reply check rejects a Claude reply as `malformed_output`, `scripts/security_audit.sh` now resumes the same Claude session once and asks for the JSON array alone, instead of failing the audit straight away.

Security Audit run 40333816067 audited 15 files, found nothing, and replied `[]` followed by a summary of what it checked. The reply check rejects that by design, because a `[]` followed by prose is never read as a clean audit, so under `AI_ENGINE_FALLBACK_POLICY=capacity` the run failed and sent a `refused Claude fallback (non-capacity)` warning. The repair turn answers from the audit the session already did, and its reply goes through the same strict check, so the rule against prose around `[]` is unchanged. The repair runs only when the audit's session is in the job's session store, so it never starts a new session that audited nothing. A repair that is rejected, fails or cannot run falls back as `malformed_output`, exactly as before. The one exception is a repair whose own Claude call falls back (exit 75, 76 or 80, for example a capacity wait): the audit then records that reason instead, so a capacity wait during the repair still exits 80. The repair prompt also tells the model not to answer `[]` for an audit that did not finish, so an unfinished audit still fails.

| The numbers that matter | Value |
| --- | --- |
| Repair turns per audit | at most 1, only after `malformed_output` |
| New log line | `SECURITY_AUDIT_CLAUDE_REPAIR outcome=accepted\|rejected\|failed\|skipped reason=` |
| Run that triggered the fix | Security Audit run 40333816067 (PR #8183) |

What this means for operators: a Claude audit that slips on the output format now finishes on its own in the same run, at the cost of one short resumed call, instead of failing and waiting for the next dispatch. `SECURITY_AUDIT_CLAUDE_REPAIR` in the job log shows when that happened.

### For contributors

The reply check moved unchanged into `security_audit_check_claude_reply <reply_file>`, and the repair is `security_audit_repair_claude_reply`. `missing_output` and `schema_mismatch` get no repair. Tests: `test_security_audit_claude_repair_*` in `tests/test_security_audit_workflow_contract.py`.
