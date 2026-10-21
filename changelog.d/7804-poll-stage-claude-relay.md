<!-- changelog: fixed -->
- **The orchestrator poll's security pass runs on the Claude engine again.** `orchestrate_poll.yml` now stages `scripts/claude_anthropic_relay.py` next to `codex_isolated_exec.sh`.

Since #6891, `codex_isolated_exec.sh` refuses `engine=claude` runs unless `claude_anthropic_relay.py` sits next to it. The poll's "Stage workflow support files" step copied the helper without the relay, so in consumer repos every project security pass logged `CODEX_ISOLATION unavailable engine=claude reason=support_missing`, the codex fallback was refused (`AI_ENGINE_FALLBACK_REFUSED role=SECURITY_AUDIT reason=isolation_unavailable`), and the pass ended with `SECURITY_PASS_FAILED reason=engine_unavailable`. Tracking projects could not complete. The other reusable workflows already staged the relay.

What this means for consumer repos: after the next `@stable` sync, the security pass on Claude projects runs instead of failing on every poll, and those projects can complete.

### For contributors

`tests/test_codex_agent_isolation_contract.py` now reads both `required_support` lists from `codex_isolated_exec.sh` and fails if any workflow step that stages the helper leaves out a file on either list.
