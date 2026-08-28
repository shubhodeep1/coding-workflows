<!-- changelog: fixed -->
- **The OpenCode live smoke now proves reasoning delivery for adaptive and detail-less models instead of false-failing them.** `opencode-live-smoke.yml` uses a reasoning-demanding probe prompt and accepts provider reasoning text as fallback evidence when the endpoint omits reasoning token counts.

The first all-slug run of the Phase 1 rollout gate (run 33087059507) failed two of eight slots with `no_reasoning_usage` even though both models reason correctly in production. Direct probes against OpenRouter showed two distinct causes: `openai/gpt-5.6-sol` treats reasoning effort as a ceiling and performs zero reasoning on the old trivial `Return exactly OK` prompt on every endpoint and parameter shape, and `deepseek/deepseek-v4-pro` reasons on every call but OpenRouter's `chat/completions` usage omits `completion_tokens_details`, so its count can never exceed zero. The smoke's probe prompt now embeds a small verification task that adaptive models actually reason about, and a zero token count falls back to one non-streaming `chat/completions` probe that accepts non-empty reasoning text for the same model, prompt, and `xhigh` effort. The per-slot table reports the fallback path as `PASS(text)`, and a slot with neither token usage nor reasoning text still fails as `no_reasoning_usage`.

| The numbers that matter | Value |
| --- | --- |
| Slots false-failing before this fix | 2 of 8 (`deepseek/deepseek-v4-pro`, `openai/gpt-5.6-sol`) |
| Extra API calls per fallback | 1 non-streaming `chat/completions` probe |
| Files changed | `.github/workflows/opencode-live-smoke.yml`, `tests/test_opencode_live_smoke_workflow.py` |

What this means for operators: dispatching `opencode-live-smoke.yml` without a model filter can now genuinely go all-green, which is the recorded evidence the opencode cutover's read-side and write-side phases are gated on.

### For contributors

`scripts/write_opencode_config.sh` is unchanged: wire captures confirmed opencode 1.18.23 delivers the configured `reasoning: {effort}` variant to OpenRouter exactly as written, so the P1 configuration writer was never the defect.
