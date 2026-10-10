<!-- changelog: changed -->
- **The review panel swaps two slots: a Claude slot on the account pool replaces `mistralai/mistral-small-2603`, and `z-ai/glm-5.3-flash` replaces `deepseek/deepseek-v4-pro`.** The Claude slot never calls OpenRouter, and the two swaps cut the panel's OpenRouter spend by about $20 a day (DeepSeek at about $34 a day against an estimated $14 for GLM-5.3 Flash).

`REVIEWER_MODELS` in `review_autofix.yml` (and the `opencode-live-smoke.yml` roster) now lists `anthropic/claude-sonnet-5.5` and `z-ai/glm-5.3-flash` in the second and third slots. The Claude slot runs `claude-sonnet-5-5` through the Claude Code CLI in a read-only `prepare-ephemeral claude` review sandbox, the same pattern the review-blocked judge uses, under the new engine role `PANEL_REVIEWER`. When the Sonnet run does not succeed (crash, timeout, refusal, malformed output, or a usage limit on every account), the slot retries once on `REVIEWER_POOL_FALLBACK_MODEL` (default `claude-haiku-5-5`) in the same pool. When the pool itself is unavailable (role resolved to codex, no credential, sandbox not prepared), the slot ends `skipped_pool` and the round runs without it; there is no OpenRouter fallback. GLM-5.3 Flash fails back to `z-ai/glm-5.3-flashx`. The one-reviewer tier's rescue, which reran a skipped or context-overflowing sole reviewer on `openai/gpt-6-luna`, now covers any sole slot instead of Mistral only.

| The numbers that matter | Value |
| --- | --- |
| Mistral calls rate-limited upstream, Oct 7-9 | 514 of 514 |
| Rounds in which the Mistral slot produced a review (Oct 7-10) | 11 of 290 |
| DeepSeek V4 Pro cost per review round / per day | about $0.74 / $34 |
| GLM-5.3 Flash estimated cost per round | about $0.30 |
| GLM-5.3 Flash output price vs `z-ai/glm-5.2` (the Sep-Oct overrun) | $0.50 vs $7.00 per M tokens |
| New status / role / variables | `skipped_pool`, `PANEL_REVIEWER`, `REVIEWER_POOL_FALLBACK_MODEL`, `AI_ENGINE_PANEL_REVIEWER` |

What this means for operators: each review round now uses about two Sonnet runs on the Claude account pool, one per review pass, so watch the hourly pool near-cap alert. `AI_ENGINE_PANEL_REVIEWER=codex`, or a PR's `ai:codex` label, turns the Claude slot off. Its pool runs log `CLAUDE_POOL run role=PANEL_REVIEWER ...` and a `REVIEWER_POOL: slot=... status=...` line in the job log.

### For contributors

The slot is dispatched by `reviewer_is_pool_slot` / `run_pool_reviewer` in `scripts/review_run_reviewers.sh` and writes the same `status_` / output / log files as `run_reviewer`. `skipped_pool` counts as a fail-open skip, like `skipped_unmapped` and `skipped_open`. `scripts/review_untrusted_sandbox.sh` admits `PANEL_REVIEWER` only with read access. Both new models have `scripts/codex_model_catalog.json` entries. Tests: `tests/test_review_pool_reviewer.py`.
