# Codex System Instructions (Production Code + MongoDB)

These instructions are **mandatory** and must be followed **before any action**.

---

## PRE-TASK MANDATORY CONTEXT LOADING

Before any task, read:
- `README.md`
- `agents.md`
- all `/db/contracts/*.yml` (or `.json`) relevant to collections that may be touched

If any are missing or unclear: **STOP and ask using the mandatory Q/A format.**
Never assume undocumented behavior.

---

## Serena (MCP) Semantic Tooling (MANDATORY when available)

Reduce token usage by using Serena's LSP-backed semantic tools instead of full-file reads/writes.

Rules:
- ALWAYS use Serena semantic tools over full-file reads.
- NEVER read an entire file if symbol tools suffice.
- NEVER rewrite an entire file if `replace_symbol_body` or `insert_after_symbol` can do it.

### Reading code (use INSTEAD of cat/read):
- `mcp__serena__get_symbols_overview` — file structure (classes, functions, exports)
- `mcp__serena__find_symbol` — jump to a symbol definition
- `mcp__serena__find_referencing_symbols` — find all callers/usages
- `mcp__serena__search_for_pattern` — regex search (replaces grep)

### Editing code (use INSTEAD of full-file writes):
- `mcp__serena__replace_symbol_body` — replace a function/class body
- `mcp__serena__insert_after_symbol` / `insert_before_symbol` — add code around a symbol
- `mcp__serena__rename_symbol` — rename across codebase (LSP refactor)

### Workflow:
1. `get_symbols_overview` → understand file structure
2. `find_symbol` → drill into specific functions
3. `find_referencing_symbols` → understand change impact
4. Edit with `replace_symbol_body` / `insert_after_symbol` — NOT full-file rewrites

### Search result limits:
- Serena results may truncate at ~29k chars. Do NOT re-run via shell grep. Instead narrow the query or split into targeted lookups.

### Fallback:
- If Serena is unavailable or errors, fall back to normal file reads/writes. Do not stall.

## Context7 Library Docs (OPTIONAL when available)

Use Context7 only when library/framework API details are uncertain and current docs are needed.

Rules:
- Resolve the library first (`mcp__context7__resolve-library-id`).
- Then fetch targeted docs (`mcp__context7__query-docs`) for the exact API surface being changed.
- If naming differs across environments, use the exact Context7 doc-query tool name exposed in the current tool list.
- Keep normal Serena-first code navigation/editing workflow for repository semantics.
- If Context7 is unavailable or errors, continue without it. Do not block implementation.

## OpenRouter Prompt Cache Instrumentation

- Default behavior uses `OPENROUTER_PROMPT_CACHE_DISABLED=false`.
- Keep prompt assembly cache-friendly: stable static prefix first, dynamic issue/PR/runtime suffix second.
- Add explicit `cache_control: { type: "ephemeral" }` only for direct OpenRouter HTTP calls that already exist.
- Skip explicit cache breakpoint injection for Gemini-family model IDs.
- Normalize usage telemetry into:
  - `cache_creation_input_tokens`
  - `cache_read_input_tokens`
  - `prompt_tokens`, `completion_tokens`, `total_tokens`
- Enforce fail-open behavior: if cache metadata is rejected or unavailable, continue without failing the workflow.

---

## 0. Prime Directive (NON-NEGOTIABLE)

If you are **not 100% certain** the outcome matches the user's expectations:
**STOP. ASK. DO NOT PROCEED.** — even if the task looks trivial or the intent seems obvious.

---

## 1. Core Priorities (Strict Order)

1. Security
2. Correctness & safety
3. Backward compatibility
4. Operational clarity
5. Performance
6. Speed

---

## 2. Always-On Ask-First Mode

Ambiguity is a **hard stop**.

Before asking questions:
- Restate your understanding of the task
- Study the repo to avoid avoidable questions
- Identify all blocking uncertainties

Ask clarifying questions **before** modifying code, schemas, configs, scripts, docs, migrations, or infrastructure.

### Clarification Batching
Ask **all known questions in a single batch**. Follow-ups only if answers introduce new ambiguity.

### Mandatory Question Format

Use stable identifiers `Q1`, `Q2`, etc. with letter-only answers (`A`, `B`, `C`, or `A+C`).

**Format:**

> **Q1: \<question\>**
>
> Choices:
> - **A** — \<description\> (RECOMMENDED)
> - **B** — \<description\>
> - **C** — \<description\>
>
> Reply: `Q1: A`

Rules:
- One decision per Q-ID. Never bundle multiple decisions.
- Mark at least one option `(RECOMMENDED)`.
- Do NOT use numeric question numbering (1, 2, 3) — only Q-IDs.
- If multiple selections allowed, state explicitly.

### When to Ask

Ask if **any** of these are unclear:
- **Scope:** which repo/module/service, runtime vs batch, prod/staging/dev
- **Behavior:** expected behavior, edge cases, failure handling, safety constraints
- **Interfaces:** API/CLI/env vars, backward compatibility, logging/observability
- **Data/MongoDB:** collections, uniqueness rules, index contracts
- **Operations:** timing, concurrency, rollback/recovery

### Forbidden
- Guessing intent or applying "reasonable defaults" without confirmation
- Silent refactors, cleanups, or speculative fixes

---

## 3. Production Code Assumptions

All code is production-bound. Verify: logic correctness, error paths, race conditions, idempotency, deployment safety.

---

## 4. Environment Variables

- Always provide defaults for new env vars unless explicitly told otherwise.
- Preserve all existing env var names.
- Batch controls in this repo: `BATCH_API_DISABLED` (default `false`), `BATCH_API_PROVIDER` (default `auto`), `BATCH_API_POLL_TIMEOUT_HOURS` (default `24`).
- Implement repair control in this repo: `MAX_POST_CODEX_REPAIR_ATTEMPTS` (default `1`) for the in-job post-syntax-failure Codex repair loop.

---

## 5. Minimal Change Set

- Do NOT change formats, types, or unrelated logic.
- Do NOT reformat files unless required for the fix.
- Do NOT create test scripts unless asked.
- Extend existing mechanisms — never compete with them.

---

## 6. Backward Compatibility / Naming Immutability

NEVER rename, remove, or repurpose existing identifiers (variables, functions, classes, modules, CLI flags, env vars, URL paths, JSON/DB fields, index/event/metric names, log keys) without asking first and detailing current usage.

All renames are **breaking changes**. If a new name is needed:
- Add alongside the old one, accept both inputs, preserve old outputs, document aliases.

---

## 7. Output Requirements

In every final response:
- List all files changed with line ranges of major logic changes (skip formatting-only)
- If behavior changes: update `README.md` / `agents.md` with env vars, DB behavior, indexes, operational steps, failure modes

---

## 8. Debugging & Diagnostics

If a problem's cause is unclear: add **diagnostic logging first**, not speculative fixes.
Logging must be structured, searchable, with context keys.

---

## 9. Code Style

- **Tabs** for indentation — EXCEPT in formats where the language forbids tabs or mandates a different indentation token:
	- **YAML** (`.yml`, `.yaml`) MUST use **2-space** indentation. YAML spec disallows tab characters as indentation; `docker compose config` and every YAML parser will reject tab-indented YAML.
	- Makefile recipe bodies must use a literal TAB (this is a Make requirement, not a style choice).
	- If a sub-directory pins a different convention via `.editorconfig`, honour that file for files it covers.
- Opening braces on a **new line**

---

## 10. MongoDB Rules

### A) DB Contract
One contract per collection at `/db/contracts/<collection>.yml`. Must include: collection name, indexes (keys, uniqueness, partials, collation), purpose, business invariants, write entrypoints. Any query/write change must update the contract.

### B) Index Registry
Single shared index module (e.g. `ensureIndexes`). No ad-hoc `createIndex` calls.

### C) Runtime Index Creation
Use distributed lock via `_locks` collection with lease expiry. Compare indexes by name+keys+options. Never silently drop/recreate in prod.

### D) Unique Index Safety
Explicit null/missing/empty rules. Prefer partial unique indexes. Preflight duplicate detection. Treat E11000 as expected in races.

### E) Idempotency
Require idempotency keys backed by unique indexes. Prefer atomic upserts.

### F) Transactions
Use sparingly, retry transient errors, keep scope minimal.

### G) Query/Index Alignment
Every query must have a matching index or documented justification.

### H) Operational Safety
Document index timing, expected output, failure modes, rollout considerations.

---

## 11. Task Checklist Completion Gate

When a user provides a task list for execution, convert it to a checklist.

Rules:
- Track and update checklist visibly in conversation
- Mark items complete only after work is done or user confirms
- Map every task; never skip or silently drop items
- Complete all non-PR items before creating a PR (unless user approves splitting)
- If blocked: report failure, keep item open, await direction

Scope: In PR review mode, applies only to new task lists in the current request.

---

## 12. PR Review Mode

When the user comments `@codex change` in a PR: review all feedback and apply only explicitly requested changes.

### Intent Preservation (NON-NEGOTIABLE)
- Do NOT deviate from original project intent
- Do NOT introduce new goals, scope, abstractions, or behaviors unless approved
- Treat existing implementation as intentional

### Ambiguous Feedback
If feedback could change behavior, broaden/narrow scope, or alter semantics: **STOP and ask (Q/A format)** before acting.

### Forbidden
- "Improving" design beyond the comment
- Refactoring for elegance or style
- Applying suggestions that conflict with existing behavior without surfacing the conflict

### Acceptance Criteria
After changes: original intent preserved, behavior unchanged unless approved, backward compatible, no new assumptions, changes traceable to PR comments. If no changes needed: reply "No changes are needed."

---

## 13. Workflow Log Analysis Batch Operations

- `workflow-log-analysis.yml` uses artifact-backed deferred polling with `workflow-log-analysis-batch-state` (`workflow_log_analysis_batch_state.json`).
- Pending batch analyzer exits with code `3` to signal deferred completion; workflow must treat this as non-failure.
- On unsupported provider/model, capability probe errors, poll timeout, or batch terminal errors, analyzer must emit structured `batch_fallback` warnings and run synchronous analysis.
- `memory_maintenance.yml` currently has no LLM path; keep compaction behavior unchanged and emit `batch_noop` compatibility logging only.

---

## 14. Repository Hygiene

- Never write into `.git/**` (no artifacts, caches, or bytecode).
- Set `PYTHONDONTWRITEBYTECODE=1` for Python tooling on repo files.
- Treat `__pycache__`/`*.pyc` under `.git/` as invalid state.

---

## 15. Semantic Cache Scope

- Semantic cache integration is allowed only in `clarify` and `orchestrate_clarify_respond`.
- Do NOT add semantic cache hooks to `implement`, `review_autofix`, `validate`, `plan`, or `orchestrate` unless explicitly approved.
- Cache key basis must use issue body + full issue thread history.
- Cache hit audit logs must include: `phase`, `similarity`, `cached_at`, `original_issue_id`.
- Any semantic cache failure must fail open (warning + continue normal OpenRouter/Codex execution).

---

## 16. Validation Self-Healing

- `scripts/validate_process.sh` attempts to self-heal prompt-wording defects in the four validation prompts (`mode-validate-{discover,generate,fix-harness,diagnose}.txt`) before burning a `MAX_VALIDATE_CYCLES` cycle. The self-heal flow is driven by `prompts/mode-validate-self-heal.txt` and `scripts/self_heal_validation.sh`.
- The budget is `MAX_SELF_HEAL_ATTEMPTS` (default `2`) per `validate_process.sh` invocation. Self-heal re-execs do NOT increment `VALIDATION_CYCLE`.
- Self-heal is ONLY permitted to edit the four validation prompt files. Do not extend it to scripts, workflow YAML, or other prompts without a new ask-first decision.
- On a successful healed pass, `validate_process.sh` POSTs `repository_dispatch` (event type `validation-prompt-self-heal`) to `shubhodeep1/coding-workflows` with the accumulated patches. The intake workflow `.github/workflows/validation-improvements-intake.yml` opens a **draft** PR with the `[skip ai]` title token and label `ai:needs-prompt-review`, and appends a ledger entry to `docs/validation-improvements.md`.
- Draft PRs are already skipped by `review_autofix.yml` (gate at line ~87), so the automated review/autofix/automerge pipeline cannot merge a self-heal PR without a human marking it "Ready for review". That event is handled by `workflow-templates/ai-review.yml` on the `ready_for_review` trigger.
- Admin alerts for each intake are sent via `tg_send_msg` (severity `WARNING`). See README.md section "Validation self-healing" for the full flow and the unlock procedure.
- Before changing any of the self-heal wiring or touching `prompts/mode-validate-self-heal.txt`, re-read the hard constraints section of that prompt — the allow-list of four target files, the no-rename rule, and the no-schema-change rule are all required for correctness.

---

## FINAL REMINDER

If uncertainty exists: **ASK (multiple-choice). DO NOT EXECUTE.**

Accuracy > speed. Safety > convenience. Backward compatibility is mandatory.
