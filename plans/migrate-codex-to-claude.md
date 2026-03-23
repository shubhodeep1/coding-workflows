# Migration Plan: Codex CLI → Claude Code CLI

**Status:** Draft
**Date:** 2026-03-23

## Overview

Replace the Codex CLI (`@openai/codex`) and OpenAI editor model (`openai/gpt-5.3-codex`)
with the Claude Code CLI (`@anthropic-ai/claude-code`) and `anthropic/claude-opus-4-6`
across the clarify, plan, implement, and review-autofix workflows.

**Key constraints:**
- Continue using **OpenRouter** as the API provider (no direct Anthropic API calls).
- Editor model becomes `anthropic/claude-opus-4-6` at `xhigh` reasoning effort.
- The **multi-model review process is unchanged** — only the editor/autofix model swaps.
- Serena MCP integration must continue working.

---

## 1. What Changes

### 1.1 CLI Package

| | Before | After |
|---|--------|-------|
| Package | `@openai/codex@v0.114.0` | `@anthropic-ai/claude-code@latest` |
| Install | `npm install -g @openai/codex@"v0.114.0"` | `npm install -g @anthropic-ai/claude-code` |
| Binary | `codex` | `claude` |

**Files to update (installation step):**
- `.github/workflows/clarify.yml` — line ~30
- `.github/workflows/plan.yml` — line ~53 (+ npm cache key on line ~42)
- `.github/workflows/implement.yml` — line ~63
- `.github/workflows/review_autofix.yml` — line ~87
- All `internal-*.yml` variants that reference the above

### 1.2 CLI Invocation

Codex invocation pattern:
```bash
cat "${PROMPT_FILE}" | codex exec \
  --model "${MODEL_EDITOR}" \
  --full-auto \
  > "${OUTPUT_FILE}"
```

Claude Code equivalent:
```bash
claude -p \
  --model "${MODEL_EDITOR}" \
  --output-format text \
  < "${PROMPT_FILE}" \
  > "${OUTPUT_FILE}"
```

Key differences:
- `codex exec --full-auto` → `claude -p` (print/pipe mode, non-interactive).
- `--mcp-config ~/.codex/mcp.json` → `--mcp-config ~/.claude/mcp.json` (or set via `CLAUDE_MCP_CONFIG` env var — verify CLI docs).
- Claude Code reads stdin natively with `claude -p`.

**Files to update (invocation):**
- `clarify.yml` — line ~214
- `plan.yml` — line ~410
- `implement.yml` — line ~398
- `review_autofix.yml` — lines ~998 (reviewer), ~1457 (editor), ~1906 (conflict resolver)

### 1.3 Configuration File

Codex uses `~/.codex/config.toml`:
```toml
model_provider = "openrouter"
model = "openai/gpt-5.3-codex"
model_reasoning_effort = "xhigh"
web_search = "live"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
```

Claude Code uses environment variables (no config file needed):
```bash
export ANTHROPIC_BASE_URL="https://openrouter.ai/api/v1"
export ANTHROPIC_AUTH_TOKEN="${OPENROUTER_API_KEY}"
```

`ANTHROPIC_BASE_URL` routes all API calls through OpenRouter.
`ANTHROPIC_AUTH_TOKEN` sets the `Authorization: Bearer` header using the
existing `OPENROUTER_API_KEY` secret — no new secrets required.

These environment variables are set in the workflow `env:` block for each job,
replacing the `~/.codex/config.toml` creation steps entirely.

**Files to update (config creation blocks):**
- `clarify.yml` — lines ~126-144
- `plan.yml` — lines ~290-308
- `implement.yml` — lines ~304-323
- `review_autofix.yml` — lines ~140-154

### 1.4 Default Editor Model

| | Before | After |
|---|--------|-------|
| Model ID (OpenRouter) | `openai/gpt-5.3-codex` | `anthropic/claude-opus-4-6` |
| Reasoning effort | `xhigh` | `xhigh` |

**Files to update:**
- `implement.yml` — line ~15 (global env default)
- `clarify.yml` — line ~23 (env default)
- `plan.yml` — line ~36 (env default)
- `review_autofix.yml` — editor model default
- `README.md` — lines ~37, ~221 (docs for `WORKFLOW_EDITOR_MODEL`)

The `WORKFLOW_EDITOR_MODEL` repository variable still works as an override —
its default value simply changes.

### 1.5 Serena MCP Integration

`scripts/setup_serena.sh` currently:
1. Accepts `--context codex` and writes Serena config into `~/.codex/config.toml`
   (lines ~226-240).
2. Checks `codex --help` for MCP support (lines ~246-260).

Changes needed:
- Add `--context claude-code` support (the script already has a placeholder for
  this value — verify and complete the implementation).
- Write MCP config to `~/.claude/settings.json` or an MCP config file that
  Claude Code can consume.
- Update the health check to call `claude --help` or equivalent.
- Update all workflow calls from `--context codex` to `--context claude-code`:
  - `clarify.yml` — line ~155
  - `plan.yml` — line ~320
  - `implement.yml` — line ~302
  - `review_autofix.yml` — line ~166

### 1.6 Review / Autofix

The multi-model reviewer list in `review_autofix.yml` (lines ~28-36) is **unchanged**:
```yaml
REVIEWER_MODELS: |
  minimax/minimax-m2.5
  google/gemini-3-flash-preview
  moonshotai/kimi-k2.5
  deepseek/deepseek-v3.2
  z-ai/glm-5
  qwen/qwen3.5-397b-a17b
  openai/gpt-5-mini
```

**Important limitation:** Claude Code CLI is designed for Claude-family models
only. It cannot reliably invoke arbitrary third-party models (deepseek, gemini,
qwen, etc.) even when routed through OpenRouter. This affects the reviewer
invocations in `review_autofix.yml` (line ~998) which call `codex exec --model
"${model}"` for each reviewer model.

**What changes and what stays:**

| Role | Before | After |
|------|--------|-------|
| Reviewer (7 models) | `codex exec --model "${model}"` | **Keep using `codex exec`** — these are non-Claude models |
| Editor/Autofix | `codex exec --model "${MODEL_EDITOR}"` | `claude -p --model "${MODEL_EDITOR}"` |
| Conflict resolver | `codex exec --model "${MODEL_EDITOR}"` | `claude -p --model "${MODEL_EDITOR}"` |

This means `review_autofix.yml` will install **both CLIs**: Codex for the
multi-model reviewer loop, and Claude Code for the editor/autofix and conflict
resolution steps. The Codex installation can be dropped entirely once the
reviewer invocations are migrated to direct OpenRouter API calls (`curl`) in a
future iteration.

**Future simplification (out of scope for this migration):**
Replace `codex exec` reviewer calls with direct `curl` calls to the OpenRouter
chat completions endpoint. This would eliminate the Codex CLI dependency entirely:
```bash
curl -s https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer ${OPENROUTER_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"model":"'"${model}"'","messages":[{"role":"user","content":"..."}]}'
```

### 1.7 System Instructions & Prompts

These files reference "Codex" by name but are model-agnostic in substance.
Rename/rebrand references for clarity:

| File | Change |
|------|--------|
| `codex_system_instructions.md` | Rename to `claude_system_instructions.md`; update any Codex-specific language |
| `unattended_llm_system_instructions.md` | Update references from "Codex" to "Claude" |
| `prompts/header.txt` | Update reference to `codex_system_instructions.md` → `claude_system_instructions.md` |
| `prompts/mode-clarify.txt` | No changes expected (model-agnostic) |
| `prompts/mode-plan.txt` | No changes expected |
| `prompts/mode-implement.txt` | Update reference if it mentions `codex_system_instructions.md` |

### 1.8 Documentation

| File | Change |
|------|--------|
| `README.md` | Update CLI name, default model, installation instructions, quickstart |
| `ai_pipeline.md` | Update any references to Codex CLI |
| `serena_implementation_plan.md` | Update Codex references in integration sections |
| `CHANGELOG.md` | Add migration entry |

### 1.9 npm Cache Keys

`plan.yml` caches the Codex CLI install (line ~42) using cache key `codex-v0.114.0`.
Update to cache Claude Code CLI with an appropriate key.

---

## 2. What Does NOT Change

- **OpenRouter as API provider** — all LLM calls still go through OpenRouter.
- **`OPENROUTER_API_KEY` secret** — same secret, same provider.
- **Multi-model reviewer list** — all 7 reviewer models stay as-is.
- **Serena MCP server** — same Serena version, same tools, same LSP approach.
- **AI memory system** — fully model-agnostic, no changes.
- **Label state machine** — `/answer`, `/approved`, `/reclarify` commands unchanged.
- **GitHub Actions structure** — reusable workflows, consumer repo wrappers unchanged.
- **Pipeline phases** — Clarify → Plan → Implement flow unchanged.
- **Prompt content** — the actual instructions, output contracts, and decision
  formats are model-agnostic and stay the same.
- **Python utility scripts** — `ai_memory_lib.py`, `ai_context_utils.py`, etc.

---

## 3. Secrets & Variables

### Secrets (no change)
| Secret | Status |
|--------|--------|
| `OPENROUTER_API_KEY` | **Keep** — still routing through OpenRouter |
| `GH_PAT` | **Keep** — unchanged |
| `TELEGRAM_*` | **Keep** — unchanged |

### Repository Variables
| Variable | Before | After |
|----------|--------|-------|
| `WORKFLOW_EDITOR_MODEL` | `openai/gpt-5.3-codex` (default) | `anthropic/claude-opus-4-6` (default) |
| `SERENA_VERSION` | No change | No change |
| `SERENA_DISABLED` | No change | No change |
| `SERENA_LANGUAGES` | No change | No change |

Consumer repos that explicitly set `WORKFLOW_EDITOR_MODEL` to an OpenAI model
will need to update their variable to `anthropic/claude-opus-4-6` (or remove it
to pick up the new default).

---

## 4. Migration Checklist

### Phase 1: Preparation
- [ ] Verify `claude -p` stdin/stdout behavior matches `codex exec` output format
- [ ] Verify Claude Code MCP config format for Serena integration
- [ ] Pin a specific Claude Code CLI version for reproducibility

### Phase 2: Core Changes (clarify, plan, implement)
- [ ] Add Claude Code CLI installation step in `clarify.yml`, `plan.yml`, `implement.yml`
- [ ] Replace `codex exec` invocations with `claude -p` in all three workflows
- [ ] Replace `~/.codex/config.toml` creation with `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` env vars
- [ ] Update default model from `openai/gpt-5.3-codex` to `anthropic/claude-opus-4-6`
- [ ] Update `scripts/setup_serena.sh` for `--context claude-code` (MCP config path + health check)
- [ ] Update Serena context arg in workflow calls from `--context codex` to `--context claude-code`
- [ ] Remove Codex CLI installation from `clarify.yml`, `plan.yml`, `implement.yml`

### Phase 3: Review/Autofix Workflow
- [ ] Keep Codex CLI installation for multi-model reviewer loop (non-Claude models)
- [ ] Add Claude Code CLI installation alongside Codex in `review_autofix.yml`
- [ ] Replace editor/autofix `codex exec` calls with `claude -p` (lines ~1457, ~1906)
- [ ] Replace config.toml creation with env vars for Claude Code
- [ ] Update editor model default to `anthropic/claude-opus-4-6`

### Phase 4: Instructions & Docs
- [ ] Rename `codex_system_instructions.md` → `claude_system_instructions.md`
- [ ] Update all references to the renamed file in prompts and workflows
- [ ] Update `unattended_llm_system_instructions.md` Codex → Claude references
- [ ] Update `README.md` (model, CLI, quickstart)
- [ ] Update `ai_pipeline.md` CLI references
- [ ] Update npm cache keys in `plan.yml`
- [ ] Add CHANGELOG entry

### Phase 5: Internal Variants
- [ ] Update all `internal-*.yml` workflows to match main workflow changes

### Phase 6: Validation
- [ ] Run internal-clarify workflow on a test issue
- [ ] Run internal-plan workflow on a test issue
- [ ] Run internal-implement workflow on a test issue
- [ ] Run internal-review workflow on a test PR (verify both CLIs work)
- [ ] Verify Serena MCP tools are invoked successfully
- [ ] Verify consumer repo wrappers work with `@stable` tag after release

---

## 5. Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Output format differences between `codex exec` and `claude -p` | Medium | Test each phase; adjust output parsing (STATUS/QUESTIONS markers) if needed |
| MCP config format differs between Codex and Claude Code | Medium | Review Claude Code MCP docs; update `setup_serena.sh` accordingly |
| Claude opus-4-6 output style differs from gpt-5.3-codex | Low | Prompts are explicit about output contracts; minor prompt tuning may be needed |
| Two CLIs in review_autofix.yml adds install time | Low | Codex is only needed for reviewer loop; can be replaced with `curl` calls later |
| Consumer repos break if they hardcoded model name | Low | Document in CHANGELOG; `WORKFLOW_EDITOR_MODEL` override still works |

---

## 6. Rollback Plan

If issues arise after migration:
1. Revert the workflow changes (single commit revert).
2. The `OPENROUTER_API_KEY` and OpenRouter infrastructure remain unchanged,
   so reverting to Codex CLI + `openai/gpt-5.3-codex` is a clean rollback.
3. Consumer repos on `@stable` tag are unaffected until a new stable tag is cut.
