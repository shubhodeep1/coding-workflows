<!-- changelog: changed -->
- **Four review-autofix reviewer slots move to the latest models in their families.** The default `REVIEWER_MODELS` roster in `.github/workflows/review_autofix.yml` now runs `minimax/minimax-m3`, `moonshotai/kimi-k3`, `qwen/qwen3.7-plus`, and `x-ai/grok-4.6` in place of `minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, `qwen/qwen3.6-plus`, and `x-ai/grok-4.20`.

The `moonshotai/kimi-k2.5` swap is a fix as much as an upgrade: Moonshot discontinued the kimi-k2 series upstream on 2026-05-25, so that reviewer slot has been pointing at a retired model. The other three swaps track each family's current production release (MiniMax M3 and Kimi K3 both bring 1M-token context; Grok 4.6 replaces the February `grok-4.20` beta). The review-tier defaults follow the roster: `REVIEW_TIER_LITE_REVIEWER_SLUG` now defaults to `qwen/qwen3.7-plus` and `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` to `minimax/minimax-m3,deepseek/deepseek-v4-pro,x-ai/grok-4.6`. Each new reviewer gets a same-provider entry in `scripts/reviewer_failback_chains.json` (`minimax-m3 -> minimax-m2.5`, `kimi-k3 -> kimi-k2.7-code`, `qwen3.7-plus -> qwen3.6-plus`, `grok-4.6 -> grok-4.20`), and `scripts/codex_model_catalog.json` gains entries for all five new slugs. `deepseek/deepseek-v4-pro` and `mistralai/mistral-small-2603` were already the latest in their families and are unchanged, as are the `openai/gpt-5.5` editor and consolidator defaults.

| The numbers that matter | Value |
| --- | --- |
| Reviewer slots updated | 4 of 6 |
| New catalog entries | 5 (`minimax-m3`, `kimi-k3`, `kimi-k2.7-code`, `qwen3.7-plus`, `grok-4.6`) |
| New failback chains | 4 |
| Kimi K2.5 upstream retirement date | 2026-05-25 |

What this means for operators: repos overriding `REVIEWER_MODELS`, `REVIEW_TIER_LITE_REVIEWER_SLUG`, or `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` via repo vars keep their overrides; repos on the defaults pick up the new roster on the next `@stable` sync. No env var names changed.

### For contributors

The retained non-roster failback entries (`qwen3.6-plus -> qwen3-coder-plus`, `grok-4.20 -> grok-4.1-fast`) stay in `scripts/reviewer_failback_chains.json` so operator roster overrides pointing at the previous generation still fail back. `docs/codex-model-reference.md` was regenerated from the catalog via `make generate`.
