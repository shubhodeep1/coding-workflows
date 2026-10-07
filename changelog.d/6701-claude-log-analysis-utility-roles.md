<!-- changelog: changed -->
- **Workflow log analysis, the weekly retro and the other utility model roles now run on the Claude engine, with their previous engine as the fallback.** The roles `LOG_ANALYSIS`, `LOG_AUDIT`, `LOG_SUMMARY`, `RETRO`, `MATERIALITY`, `SUMMARISER` and `BEHAVIOURAL_SMOKE` now default to `claude` in `.github/ai/claude_engine.json`.

| Call site | Role | Claude model | Fallback |
| --- | --- | --- | --- |
| `workflow-log-analysis.yml` analyze, deep-audit and API-redundancy passes | `LOG_ANALYSIS`, `LOG_AUDIT` | Opus 5.5 (`high`) | the unchanged codex command (`xhigh` stays codex-only) |
| weekly retro narrative and consumer retro fan-out | `RETRO` | Sonnet 5.5 | the unchanged codex command |
| unselected-run summaries (`scripts/summarize_unselected_runs.py`) | `LOG_SUMMARY` | Sonnet 5.5 | OpenRouter, for that run and the rest |
| reviewer consensus summariser, behavioural smoke synthesiser | `SUMMARISER`, `BEHAVIOURAL_SMOKE` | Sonnet 5.5 | OpenCode in a fresh review sandbox |
| `implement.yml` AI issue summary on the PR | `SUMMARISER` | Sonnet 5.5 | the unchanged codex command |

Every Claude call runs read-only in a credential-free, network-isolated container. The log-analysis, retro and implement-summary paths use `claude_run_selected` from the trusted engine root that the workflow stages, never from a PR or integration checkout. The two review roles go only through `scripts/review_untrusted_sandbox.sh`, which now admits `SUMMARISER` and `BEHAVIOURAL_SMOKE` as read-only roles. If that sandbox cannot be prepared, or no Claude pool credential is present, they keep the unchanged read-only OpenCode command. A workflow-log-analysis dispatch from a non-default ref stays on codex, so unmerged engine support never fetches the Claude pool credential. `MATERIALITY` changes only its default: `scripts/review_agents_md_materiality.sh` still makes no model call.

To roll a role back, set the repo variable `AI_ENGINE_<ROLE>=codex` or add the `ai:codex` label. The review roles read the label and `AI_ENGINE` only, because `review_autofix.yml` does not forward the per-role variable. Fallbacks log `AI_ENGINE_FALLBACK role=<ROLE> reason=<reason>`.
