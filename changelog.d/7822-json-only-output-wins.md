<!-- changelog: fixed -->
- **Claude replies in the exact format a role prompt asks for.** `unattended_system_instructions.md` §16 no longer asks for a summary, assumptions and missing-context notes when a role prompt fixes a machine-readable reply such as "one JSON array and nothing else".

The orchestrator security pass on Claude replied `[]` and then a written summary of what it checked. `scripts/security_audit.sh` rejects that as `malformed_output`, because a `[]` followed by prose never counts as a clean audit. The pass then failed with `SECURITY_PASS_FAILED reason=engine_unavailable` and the poller sent a `refused Claude fallback (non-capacity)` warning. The cause was §16 telling every result to include those report sections, which contradicts the audit prompt. The parser is unchanged and stays strict.

| The numbers that matter | Value |
| --- | --- |
| File changed | `unattended_system_instructions.md` §16 |
| First run seen | fun-token-multi-chain poller run 39619619223, tracking issue 579 |

What this means for consumer repos: once this release syncs, security passes that Claude finds clean are accepted instead of failing as `malformed_output`, and any other role whose prompt asks for JSON only gets a parseable reply.

### For contributors

`tests/test_security_audit_prompt_policy.py::test_system_instructions_let_json_only_prompt_override_report_sections` pins both the audit prompt's JSON-only line and the §16 rule.
