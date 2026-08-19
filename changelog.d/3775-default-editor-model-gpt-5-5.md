<!-- changelog: changed -->
- **The default editor model for every codex-driven phase is now `openai/gpt-5.5`; the capacity fallback moved to `openai/gpt-5.4`.**

Every workflow that previously defaulted its model to `openai/gpt-5.4` — clarify, plan, implement (editor and diagnose), orchestrate, orchestrate-poll (judge and conflict resolver), review_autofix (editor and consolidator), validate, check-failure triage, workflow-log-analysis, validation refresh, the security audit, and the release gate's `ALT_EDITOR_MODEL` canary — now defaults to `openai/gpt-5.5`. The `WORKFLOW_EDITOR_FALLBACK_MODEL` capacity fallback in `plan.yml`, `implement.yml`, and `review_autofix.yml` moved the opposite way to `openai/gpt-5.4`, so the final retry attempt still escapes to a different OpenRouter/OpenAI per-model TPM bucket. Repo-var names are unchanged and any explicitly set repo var keeps overriding the default; the `gpt-5.4-mini` / `gpt-5.4-nano` auxiliary defaults are untouched.

| The numbers that matter | Value |
| --- | --- |
| New primary editor default | `openai/gpt-5.5` |
| New capacity-fallback default (`WORKFLOW_EDITOR_FALLBACK_MODEL`) | `openai/gpt-5.4` |
| Workflow files with swapped default literals | 13 |
| Repo-var override names changed | 0 |

What this means for operators: repos that never set `WORKFLOW_EDITOR_MODEL` (or the per-phase model vars) start running every codex phase on `gpt-5.5` at the next `@stable` sync, and a sustained `gpt-5.5` capacity crunch now falls back to `gpt-5.4` on the final retry attempt. Repos that pin models via repo vars see no change.

### For contributors

`tests/test_editor_capacity_fallback_contract.py` now pins `openai/gpt-5.4` as the fallback slug, and `scripts/codex_model_catalog.json` swaps the two entries' role descriptions (no generated-reference field changed). Historical references to the earlier `gpt-5.4` cutover and issue #3515 keep their original wording.
