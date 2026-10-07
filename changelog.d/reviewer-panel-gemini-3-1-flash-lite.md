<!-- changelog: changed -->
- **The review panel's Gemini slot is back on `google/gemini-3.1-flash-lite`.** `google/gemini-3.8-flash`, which replaced it on 2026-09-29, had become the largest single line on the OpenRouter bill.

`REVIEWER_MODELS` in `.github/workflows/review_autofix.yml` and the live-smoke roster in `.github/workflows/opencode-live-smoke.yml` now list `google/gemini-3.1-flash-lite` instead of `google/gemini-3.8-flash`. Gemini 3.8 Flash cost $2,399 of the $5,387 spent on OpenRouter from 2026-10-01 to 2026-10-04 (45%), over 53,177 requests averaging about 303K prompt tokens each. Gemini 3.1 Flash Lite lists at a third of its price. The review tiers are unchanged: the `standard` tier still runs `minimax/minimax-m3,deepseek/deepseek-v4-pro,qwen/qwen3.7-plus,openai/gpt-6-luna`, and the Gemini slot runs only on the `full` panel.

| The numbers that matter | `gemini-3.8-flash` | `gemini-3.1-flash-lite` |
| --- | --- | --- |
| List price, prompt / completion per 1M tokens | $0.75 / $3.75 | $0.25 / $1.50 |
| Cached-read price per 1M tokens | $0.075 | $0.025 |
| Observed cache hit rate | 90% (2026-09-30 to 2026-10-04) | 2% (2026-09-21 to 2026-10-03) |
| Observed requests per day as a reviewer | about 12,000 | about 175 |

What this means for operators: full-panel reviews should cost noticeably less. In its earlier stint, Flash Lite made far fewer calls per review than Flash 3.8 does, so expect shorter Gemini findings.

### For contributors

`scripts/reviewer_failback_chains.json` and `scripts/codex_model_catalog.json` keep their `google/gemini-3.8-flash` entries for operator overrides; the live slot fails back to `google/gemini-3-flash-preview`, as it did before 2026-09-29. Flash Lite's 2% cache hit rate in its earlier stint was not investigated here. `README.md` and `agents.md` no longer call the `standard` tier "the four cheapest panel models", because Flash Lite is now cheaper by list price than some of them.
