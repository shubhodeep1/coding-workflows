<!-- changelog: changed -->
- **The AI pipeline moves to the GPT-6 family, and GPT reasoning defaults drop from `xhigh` to `high`.** `WORKFLOW_EDITOR_MODEL` now defaults to `openai/gpt-6-sol`, the capacity fallback to `openai/gpt-5.6-sol`, and every lightweight utility role to `openai/gpt-6-luna`.

Every phase that follows the editor default (clarify, plan, orchestrate, the orchestrate_poll judge, clarify-respond, implement and its diagnose/repair sub-phases, the review_autofix editor, consolidator and review-blocked judge, the conflict resolver, validate, validation-refresh discovery, check-failure triage, workflow heal, security audit and workflow-log-analysis) now runs on `openai/gpt-6-sol`. `WORKFLOW_EDITOR_FALLBACK_MODEL`, which the plan, implement and review_autofix retry loops switch to on their final attempt, moves from `openai/gpt-5.5` to `openai/gpt-5.6-sol`, the previous primary. The log analyser, reviewer-consensus summariser, materiality fallback, behavioural smoke, retro and unselected-run summary roles move from `openai/gpt-5.6-luna` to `openai/gpt-6-luna`, and AI-memory keyword extraction (`AI_MEMORY_KEYWORD_MODEL`) moves from `openai/gpt-5.4-nano` to `openai/gpt-6-luna`. At the same time, 17 `THINKING_LEVEL_*` / reasoning repo-var defaults for GPT phases drop from `xhigh` to `high`, along with the matching script fallbacks.

| The numbers that matter | Value |
| --- | --- |
| New editor default | `openai/gpt-6-sol` (1.05M context, $2/$10 per Mtok up to 272K prompt tokens) |
| New capacity fallback | `openai/gpt-5.6-sol` (was `openai/gpt-5.5`) |
| New utility-role default | `openai/gpt-6-luna` ($0.10/$0.50 per Mtok, down from $0.20/$1.20 on `gpt-5.6-luna`) |
| Reasoning defaults lowered `xhigh` → `high` | 17 repo vars (`THINKING_LEVEL_CLARIFY`, `_CLARIFY_ORCHESTRATOR`, `_CLARIFY_RESPOND`, `_PLAN`, `_ORCHESTRATE`, `_JUDGE`, `_IMPLEMENT`, `_IMPLEMENT_REPAIR`, `_DIAGNOSE`, `_EDITOR`, `_REVIEW_BLOCKED_JUDGE`, `_VALIDATE`, `_CHECK_TRIAGE`, `_WORKFLOW_HEAL`, `_ANALYSIS`, `REVIEW_CONSOLIDATOR_REASONING`, `VALIDATION_DISCOVERY_REASONING_EFFORT`) |
| Kept at `xhigh` | `THINKING_LEVEL_REVIEWER` and the pass-2 large-diff level (non-GPT reviewer slots); the hardcoded security-audit and workflow-log-analysis analyze/audit passes |
| New catalog entries | 2 (`openai/gpt-6-sol`, `openai/gpt-6-luna`); the `gpt-5.6-sol` and `gpt-5.6-luna` entries stay for operator overrides |

What this means for operators: repos that do not set `WORKFLOW_EDITOR_MODEL`, `WORKFLOW_EDITOR_FALLBACK_MODEL`, the utility-role model vars, or the `THINKING_LEVEL_*` vars pick up the new models and the `high` reasoning level on the next `@stable` sync. A repo var that is already set still wins, so repos that pinned `openai/gpt-5.6-sol` or `xhigh` keep that value until the var is cleared. To restore the previous reasoning depth for one phase, set its `THINKING_LEVEL_*` var to `xhigh`.

### For contributors

`scripts/codex_model_catalog.json` gains `openai/gpt-6-sol` and `openai/gpt-6-luna` with the same fields as their gpt-5.6 counterparts (OpenRouter reports identical context windows and supported parameters), and `docs/codex-model-reference.md` is regenerated from it. The `_PROMPT_BUDGET_TOTAL_BYTES` default (800000) is unchanged; its comments now note that both the primary and fallback have 1.05M windows. The E2E smoke overrides (`low` for clarify, plan and reviewers, `medium` for the editor), the `medium` utility-role levels, and the `low` interim judge are unchanged. `CHANGELOG.md`, `analysis/` and `docs/plans/` keep their historical `gpt-5.6-sol` references.
