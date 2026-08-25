<!-- GENERATED FILE: do not edit. Run `make generate` after editing scripts/codex_model_catalog.json or scripts/codex_model_catalog_overrides.yaml. -->

# Codex model reference

This file is generated from `scripts/codex_model_catalog.json` and optional overrides in `scripts/codex_model_catalog_overrides.yaml`.
Rows with pinned override fields are marked `(frozen)` in `notes`.

| slug | default_verbosity | support_verbosity | apply_patch_tool_type | notes |
| --- | --- | --- | --- | --- |
| minimax/minimax-m2.5 | null | false | freeform | — |
| minimax/minimax-m3 | null | false | freeform | — |
| google/gemini-3-flash-preview | null | false | freeform | — |
| google/gemini-3.1-pro-preview | null | false | freeform | — |
| google/gemini-2.5-pro | null | false | freeform | — |
| moonshotai/kimi-k2.5 | null | false | freeform | — |
| moonshotai/kimi-k3 | null | false | freeform | — |
| moonshotai/kimi-k2.7-code | null | false | freeform | — |
| deepseek/deepseek-v3.2 | null | false | freeform | — |
| z-ai/glm-5 | null | false | freeform | — |
| qwen/qwen3.5-397b-a17b | null | false | freeform | — |
| qwen/qwen3-coder-plus | null | false | freeform | — |
| stepfun/step-3.5-flash | null | false | freeform | — |
| openai/gpt-5-mini | null | false | freeform | — |
| openai/gpt-5.4 | low | true | function | apply_patch_tool_type was flipped from "freeform" to "function" on 2026-05-07 after the codex#11151 ablation suite identified freeform as the root cause of announce-without-emit failures on the OpenRouter Responses path. Do not auto-rewrite. (frozen) |
| openai/gpt-5.5 | low | true | function | — |
| openai/gpt-5.4-nano | null | false | freeform | — |
| openai/gpt-5.4-mini | null | false | freeform | — |
| deepseek/deepseek-v4-pro | null | false | function | — |
| qwen/qwen3.6-plus | null | false | function | — |
| qwen/qwen3.7-plus | null | false | function | — |
| x-ai/grok-4.1-fast | null | false | function | — |
| x-ai/grok-4.20 | null | false | function | — |
| x-ai/grok-4.6 | null | false | function | — |
| mistralai/mistral-small-2603 | null | false | function | — |
