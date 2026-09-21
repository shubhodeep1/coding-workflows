<!-- changelog: changed -->
- **Three of the six review-autofix reviewer slots move to cheaper models with 1M+ token windows.** `x-ai/grok-4.6` becomes `x-ai/grok-4.20`, `moonshotai/kimi-k3` becomes `z-ai/glm-5.2`, and `mistralai/mistral-small-2603` becomes `google/gemini-3.1-flash-lite` in the `REVIEWER_MODELS` roster of `.github/workflows/review_autofix.yml`.

Across five recent successful review runs, Kimi K3 and Grok 4.6 accounted for $14.52 of the $19.09 reviewer spend that logged usage, and output tokens were only 17% and 3% of their cost respectively, so lowering reasoning effort would not have fixed it. The cost sits in input: each reviewer pass sends 170K to 400K uncached prompt tokens, Grok 4.6 charges $2.00 per million for those and $0.50 per million for cache reads, and Kimi K3 re-read 20 million cached tokens in eight calls. Mistral Small's slot was replaced for a different reason: its 262K window overflowed on most reviewer prompts and its shared OpenRouter capacity was rate-limited upstream, so it succeeded in roughly one run in eight. Every replacement keeps at least a 1M token window, and `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` plus the `opencode-live-smoke.yml` roster follow the same swap.

| The numbers that matter | Value |
| --- | --- |
| Grok slot, input / output per M tokens | $2.00 / $6.00 to $1.25 / $2.50 |
| Kimi slot, input / output per M tokens | $1.68 / $9.38 to $0.65 / $2.04 |
| Mistral slot, context window | 262K to 1M |
| Estimated Grok + Kimi spend on the five sampled runs | $14.52 to about $7.30 |
| New catalog entries | 4 (`glm-5.2`, `glm-5.3-flashx`, `grok-4.3`, `gemini-3.1-flash-lite`) |

What this means for operators: reviewer cost per PR should drop by roughly half with no change to reasoning effort, the two-pass structure, or the six-slot panel, and the Mistral slot stops burning retries and stall budget on every run. Override `vars.REVIEW_TIER_STANDARD_REVIEWER_SLUGS` or the workflow roster if a repo needs the previous models; the retired slugs stay in the catalog.

### For contributors

`scripts/reviewer_failback_chains.json` maps each new reviewer to a same-family target with a 1M+ window: `x-ai/grok-4.20 -> x-ai/grok-4.3`, `z-ai/glm-5.2 -> z-ai/glm-5.3-flashx`, and `google/gemini-3.1-flash-lite -> google/gemini-3-flash-preview`. The old `x-ai/grok-4.20 -> x-ai/grok-4.1-fast` entry was dropped because OpenRouter and models.dev no longer list that slug; the retired-roster entries for `moonshotai/kimi-k3` and `x-ai/grok-4.6` remain for operator overrides. `docs/codex-model-reference.md` was regenerated from the catalog via `make generate`.
