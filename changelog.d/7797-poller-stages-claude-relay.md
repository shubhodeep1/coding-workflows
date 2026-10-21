<!-- changelog: fixed -->
- **The AI Orchestrate Poller can run its Claude roles again.** Its "Stage workflow support files" step now copies `claude_anthropic_relay.py` next to `codex_isolated_exec.sh`.

Without the relay, every Claude run from the poller stopped with `CODEX_ISOLATION unavailable engine=claude reason=support_missing`. Under `AI_ENGINE_FALLBACK_POLICY=capacity` that meant the orchestrator security pass failed closed with `SECURITY_PASS_FAILED reason=engine_unavailable`, and each poller run sent a Telegram warning: `refused Claude fallback (non-capacity) … SECURITY_AUDIT:isolation_unavailable`. This happened even when the Claude pool had free accounts. `orchestrate.yml` had the same gap, fixed in #6908.

| The numbers that matter | Value |
| --- | --- |
| Workflow changed | `.github/workflows/orchestrate_poll.yml` |
| Consumer runs seen failing | fun-token-multi-chain 39563863015, 39564851636, 39565576987; tele-funtoken-msg-scoring 39562537641, 39563450320, 39565021848 |

What this means for consumer repos: after the next `@stable` sync, blocked security passes run on Claude on the next poller tick, and the refused-fallback warnings stop. No variable change is needed.

### For contributors

`tests/test_codex_agent_isolation_contract.py::test_orchestrate_poll_stages_every_claude_isolation_support_file` reads the Claude support files from `codex_isolated_exec.sh` and fails if the poller's required loop does not stage all of them.
