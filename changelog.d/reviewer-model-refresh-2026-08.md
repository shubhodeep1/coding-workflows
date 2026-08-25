<!-- changelog: changed -->
- **The AI pipeline moves to the latest models across every role: the editor family jumps to GPT-5.6 and four of the six reviewer slots move to their families' current releases.** `WORKFLOW_EDITOR_MODEL` now defaults to `openai/gpt-5.6-sol` in every codex-driven phase, `WORKFLOW_EDITOR_FALLBACK_MODEL` moves from `openai/gpt-5.4` to `openai/gpt-5.5`, and every `openai/gpt-5.4-mini` utility role now defaults to `openai/gpt-5.6-luna`.

The editor, consolidator, judge, clarify, plan, orchestrate, validate, security-audit, check-failure-triage, and workflow-log-analysis phases all follow the single `WORKFLOW_EDITOR_MODEL` default to `gpt-5.6-sol` (1.05M context, currently $2/$10 per Mtok promotional pricing). The capacity-fallback slot — the model each editor retry loop switches to on its final attempt — becomes `gpt-5.5`, keeping the different-TPM-bucket property while staying one generation behind the primary. The lightweight roles (release-gate log analyser, weekly retro, unselected-run summaries, reviewer consensus summariser, materiality fallback, behavioural smoke) move from `gpt-5.4-mini` to `gpt-5.6-luna` at a quarter of the input price. The reviewer roster in `review_autofix.yml` swaps `minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, `qwen/qwen3.6-plus`, and `x-ai/grok-4.20` for `minimax/minimax-m3`, `moonshotai/kimi-k3`, `qwen/qwen3.7-plus`, and `x-ai/grok-4.6` — the Kimi swap is a repair as much as an upgrade, since Moonshot retired the k2 series upstream on 2026-05-25. Review-tier defaults, reviewer failback chains, and `scripts/codex_model_catalog.json` all follow (seven new catalog entries in total), and `deepseek/deepseek-v4-pro` plus `mistralai/mistral-small-2603` stay put as already-current.

| The numbers that matter | Value |
| --- | --- |
| New editor default | `openai/gpt-5.6-sol` (1.05M context) |
| New editor fallback | `openai/gpt-5.5` (was `gpt-5.4`) |
| Utility-role default | `openai/gpt-5.6-luna` ($0.20/$1.20 per Mtok) |
| Reviewer slots updated | 4 of 6 |
| New catalog entries | 7 (`gpt-5.6-sol`, `gpt-5.6-luna`, `minimax-m3`, `kimi-k3`, `kimi-k2.7-code`, `qwen3.7-plus`, `grok-4.6`) |
| Kimi K2.5 upstream retirement date | 2026-05-25 |

What this means for operators: repos overriding any of the model repo vars (`WORKFLOW_EDITOR_MODEL`, `WORKFLOW_EDITOR_FALLBACK_MODEL`, `LOG_ANALYZER_MODEL`, `XPOLL_SUMMARISER_MODEL`, `REVIEWER_*`, `REVIEW_TIER_*`) keep their overrides; repos on the defaults pick up the new models on the next `@stable` sync. No env var names changed, and reasoning-effort defaults (`xhigh` policy) are unchanged.

### For contributors

The prompt-budget sizing comments in `scripts/review_apply_fixes.sh` and `scripts/gh_helpers.sh` now note that the 200k-token input budget is bound by the `gpt-5.5` fallback's 272k window, not the 1.05M-context primary. Retained failback entries (`qwen3.6-plus -> qwen3-coder-plus`, `grok-4.20 -> grok-4.1-fast`) stay in `scripts/reviewer_failback_chains.json` for operator roster overrides. `docs/codex-model-reference.md` was regenerated from the catalog via `make generate`.
