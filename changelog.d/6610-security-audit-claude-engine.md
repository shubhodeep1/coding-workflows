<!-- changelog: changed -->
- **The security audit now runs on the Claude engine, with codex as the fallback.** Each audit runs on Opus 5.5 at `high` effort first. It reruns on codex (`openai/gpt-6-sol`) only when Claude cannot produce a usable result.

This covers both callers: the weekly and dispatched `security-audit.yml`, and the orchestrator project security pass in `orchestrate_poll.yml`. The `SECURITY_AUDIT` role in `.github/ai/claude_engine.json` now defaults to `claude`. `scripts/security_audit.sh` resolves it with `ai_engine_for_role` and runs Claude through `claude_run`, in the same credential-free, network-isolated container that codex uses. When Claude fails, the same prompt reruns on codex and the log records `AI_ENGINE_FALLBACK role=SECURITY_AUDIT reason=<reason>`. Fallback triggers include every pool account being at the 90% usage gate (`all_gated`), no account, a crash, a timeout, and output that is missing or is not a JSON array of objects. An orchestrator project labelled `ai:codex` keeps its audit on codex. A valid empty result from Claude counts as clean, as an empty codex result does today.

| The numbers that matter | Value |
| --- | --- |
| Claude model and effort | `claude-opus-5-5`, `high` |
| Codex fallback model | `openai/gpt-6-sol` (`xhigh`, unchanged) |
| Usage gate that forces codex | every account at or above `gate_utilization` `0.9` |
| Fallback reasons logged | `all_gated`, `no_credential`, `isolation_unavailable`, `all_accounts_failed`, `timeout`, `crashed_rc_<n>`, `missing_output`, `malformed_output`, `schema_mismatch` |

What this means for operators and consumer repos: audits are billed to the Claude account pool instead of OpenRouter whenever the pool has capacity. Consumer repos pick this up on the next `@stable` sync. To keep a repository on codex, set the repo variable `AI_ENGINE_SECURITY_AUDIT=codex`. Each run's log says which engine produced its findings (`security-audit: engine=claude|codex`).

### For contributors

`claude_run` in `scripts/ai_engine.sh` accepts `AI_ENGINE_INCLUDE_PATHS` (newline-separated, default empty), which mounts trusted runtime paths read-only, as codex's `--include` does. The audit uses it for the oversized-file chunks. The output check strips at most one outer code fence before parsing. `security-audit.yml` adds `Resolve AI engine`, `Install Claude Code CLI` and `Resolve Claude credential` steps, which run only when the role resolves to Claude. `orchestrate_poll.yml` adds `SECURITY_AUDIT` to its `any_claude` role loop and passes the pool step's reason as `CLAUDE_POOL_REASON`. Tests: the Claude engine cases in `tests/test_security_audit_workflow_contract.py`, plus `tests/test_claude_engine.py` and `tests/test_orchestrator_judges_claude_engine.py`.
