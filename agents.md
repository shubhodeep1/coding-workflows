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

<!-- anchor:agents-env-vars -->
<!-- Parallel orchestrator sub-issues: append new env-var bullets to the
     bottom of this list under this anchor. Do NOT reorder existing
     bullets or reflow paragraphs — parallel edits that rewrite
     existing bullets here cause merge conflicts. -->
- Always provide defaults for new env vars unless explicitly told otherwise.
- Preserve all existing env var names.
- Batch controls in this repo: `BATCH_API_DISABLED` (default `false`), `BATCH_API_PROVIDER` (default `auto`), `BATCH_API_POLL_TIMEOUT_HOURS` (default `24`).
- Orchestrator clean-wave control: `ENABLE_CLEAN_WAVE_JUDGE_SKIP` (default `true`) skips judge invocation on clean completed waves (no failures, not stuck, project not complete) and advances wave mechanically.
- Integration-sync conflict knobs (see also section 18 below):
  - `INTEGRATION_CONFLICT_MAX_RETRIES` (default `3`) — global resolver-retry budget for non-orchestrator integration branches.
  - `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` (default `1`) — tighter budget applied **only** when the head ref matches `orchestrator/project-*`. One resolver shot, then escalate to the integration judge.
  - `FINGERPRINT_PER_FILE_CAP` (default `12`) — cap on captured patterns per file per direction.
  - `FINGERPRINT_MIN_PATTERN_CHARS` (default `12`) — minimum trimmed-line length for a captured fingerprint pattern.
- Clarify orchestrator override: `THINKING_LEVEL_CLARIFY_ORCHESTRATOR` (default `xhigh`) applies only when clarify LLM runs for `ai:orchestrator-managed` issues via forced human `/reclarify`; non-forced orchestrator-managed clarify runs skip Codex and auto-post `/answer [auto-answered-by-orchestrator]`.
- Stall recovery controls include `ENABLE_STALL_HUMAN_TERMINALIZATION` (default `false`): legacy autonomous ladder remains default; stall-judge `escalate_human` outputs are terminalization-gated to the non-human fallback action unless explicitly enabled.
- Stall-recovery PR closure (`close_linked_pr` in `scripts/orchestrate_poll_process.sh`) discovers every linked PR via three strategies before closing: (1) timeline cross-reference events, (2) head branch name matching `ai/issue-<n>`, (3) open-PR body search for `Closes|Fixes|Resolves #<n>` with a non-digit word-boundary guard. All three run every invocation and the results are deduped (`sort -u`); each open match is closed in sequence, already-closed or merged PRs are skipped, and a diagnostic line (`close_linked_pr: issue=#<n> scanned=<k> closed=<k>` or `... no linked PRs found ...`) is emitted to the workflow log. This replaces a single-path timeline-only lookup that silently missed PRs whose cross-ref event was absent (observed in prod for PR #2568 / issue #2552).
- Stall-recovery Gap-2 surfacing (`surface_reissue_closed_without_pr` in `scripts/orchestrate_poll_process.sh`) fires when stall recovery is about to close a task whose body carries the `Re-issued from #<parent>` marker AND no PR was ever produced (per `close_linked_pr`'s multi-source lookup). It emits four stable signals: (a) log prefix `REISSUE_CLOSED_WITHOUT_PR issue=<n> parent=<p> phase=<label> stall_minutes=<m> recovery_count=<c> source=<main|standalone>` — the prefix is a public contract for downstream alert greps and must not be renamed without coordinated changes; (b) GHA `::warning title=Re-issue closed without PR::`; (c) an issue comment on the re-issue before close; (d) an ai-memory run-event via `memory_record_run_event --event-type reissue_closed_without_pr` when `memory_helpers.sh` is loaded. Surfacing is fail-open and does NOT block the subsequent `close_and_reissue`; forward progress continues per Q3=A scope decision.
- Orchestrator short-circuit paths have been removed (see issue #1163). `ORCHESTRATE_SHORTCIRCUIT_MAX_CHARS` is no longer consumed. Every orchestrator run now goes through decomposition → tracking issue → integration branch → wave dispatch, regardless of description length.
- Orchestrator clarify loop guard: `ORCHESTRATOR_MAX_CLARIFY_CYCLES` (default `3`) caps auto-answer clarification cycles before escalating to `ai:blocked`. A backup comment-count guard (0 extra API calls) counts existing `/answer [auto-answered-by-orchestrator]` comments on the issue thread and blocks when the count reaches this limit even when the memory-based guard fails open. A data-provision guard (`scripts/clarify_data_provision_guard.py`) post-processes auto-answers to prevent selecting options that require external data the auto-responder cannot supply.
- Implementation no-op reissue cap: `MAX_IMPL_NOOP_REISSUES` (default `2`) limits automatic re-issues for `ai:implementation-failed` before the poller closes the issue and lets the judge verify whether work is already present.
- GitHub API rate-limit admin alert: `TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS` (default `3600`) throttles the Telegram admin alert fired from `scripts/gh_helpers.sh` when a GH API rate limit is detected. State is kept in a Telegram pinned message (marker `<!-- gh_rl_ts:EPOCH -->`) to avoid spending GH API calls on dedup. Fail-closed on pin failure. See README "GitHub API rate-limit admin alert" section.
- Orchestrator merge-conflict probe: `MAX_MERGE_DEFERRALS` (default `5`) caps how many consecutive poll cycles a single sub-PR may be deferred by `probe_sibling_merge_conflicts` in `scripts/orchestrate_poll_process.sh`. The probe uses local `git merge-tree --write-tree --name-only` against every other open sibling PR targeting the same integration branch, spending zero GH API calls per probe (one batched `gh pr list` + one batched `git fetch` per cycle). Exceeding the threshold emits a Telegram WARNING but does not fail the PR.
- Orchestrator auto-learning hot-file registry: consumer repos get partitioning with ZERO manual setup. On every detected sibling conflict, the poller appends a record to `ai-memory/orchestrator/merge_conflicts.jsonl` on the `ai-memory` branch (git protocol only, 0 GH API calls, auto-creates the branch via `memory_ensure_branch`). On the next orchestrator run, the planner step in `.github/workflows/orchestrate.yml` does `git fetch --depth=50 origin ai-memory` (git protocol, 0 GH API calls), reads the JSONL via `git show`, and unions telemetry-learned files meeting `ORCHESTRATOR_HOT_FILE_MIN_EVENTS` (default `3`) across `ORCHESTRATOR_HOT_FILE_MIN_PROJECTS` (default `2`) distinct projects within `ORCHESTRATOR_HOT_FILE_WINDOW_DAYS` (default `90`) with the optional committed seed at `.github/ai/hot_files.json`. The committed seed is OPTIONAL — consumer repos do not need to create it. Both sources missing is a valid state: the partition guard degrades to pairwise file-touch overlap detection and the poller probe still catches byte-level conflicts.
- Actions-runs cache TTL: `ACTIONS_RUNS_CACHE_TTL_SECONDS` (default `60`) controls cross-tick freshness of the shared `GET /actions/runs` snapshot persisted on the `ai-memory` branch and reused by orchestrator poll run-state readers.
- Review autofix two-pass reviewer: `ENABLE_REVIEWER_TWO_PASS` (default `true`) runs all reviewer models twice per iteration. Pass 1 uses `medium` reasoning for a broad sweep; pass 2 uses the scheduled reasoning level (default `xhigh`) with a cross-pollination summary of pass 1 findings injected into the prompt. When disabled, reviewers run a single pass at the scheduled reasoning level as before. The two-pass architecture reduces the need for multiple autofix iterations by producing more comprehensive findings in each run.
- Review autofix cross-reviewer consensus summariser: after each review pass completes, all reviewer outputs are fed as a single prompt to a codex-cli subprocess that invokes `XPOLL_SUMMARISER_MODEL` (default `openai/gpt-5.4-mini`) at `XPOLL_SUMMARISER_REASONING` (default `xhigh`) and emits one consolidated findings ledger. The ledger contains a `=== CONSENSUS FINDINGS ===` block with cross-reviewer dedup — each entry lists `flagged_by: [slug, ...]` so downstream consumers can prioritise multi-reviewer findings — followed by per-reviewer `=== FINDINGS FROM <slug> ===` sections for traceability. The pass-1 ledger (`${PREVIOUS_REVIEWS_DIR}/consensus_pass1.txt`) is wrapped with the cross-pollination header and fed into pass-2 reviewer prompts; the pass-2 ledger is written to `REVIEWER_CONSENSUS_FILE` and consumed by the editor (`scripts/review_apply_fixes.sh`) + the memory-record step. Implementation: `scripts/summarize_reviewer_consensus.sh --prefix pass1|review --output <path>` reads every `${PREVIOUS_REVIEWS_DIR}/<prefix>_*.txt`, concatenates them into one prompt, spawns `codex exec --model ${XPOLL_SUMMARISER_MODEL} --full-auto` under an **isolated** `CODEX_HOME` (mirrors the reviewer pattern at `review_run_reviewers.sh::run_reviewer`) whose `config.toml` is `sed`-patched to `model_reasoning_effort = "${XPOLL_SUMMARISER_REASONING}"`. The isolated home guarantees the `gpt-5.4-mini` override cannot leak into the editor's `openai/gpt-5.3-codex` call. The summariser retries up to 3 times on timeout / non-zero exit / empty stdout; on final failure it exits non-zero, which propagates to job failure and triggers the existing job-level Telegram failure alert at `review_autofix.yml`'s "Telegram failure" step. The editor prompt still points at the raw per-reviewer files (`${PREVIOUS_REVIEWS_DIR}/pass1_<slug>.txt` and `review_<slug>.txt`) for on-demand consultation — only the consolidated ledgers are inlined into prompts. See README "Review autofix reviewer pipeline" env var table for individual knobs (`XPOLL_SUMMARISER_LINES_PER_REVIEWER`, `XPOLL_SUMMARISER_CALL_TIMEOUT_SECS`, `XPOLL_SUMMARISER_MAX_INPUT_LINES`).
- Validation-gate final-merge budget: `MAX_FINAL_MERGE_ATTEMPTS` (default `3`). When a project reaches `mark_validation_complete` with `ai:validated`, only *budget-eligible* final-merge failures consume this counter (for example final-PR creation/lookup failure or hard merge rejection after mergeability/checks gates pass). Transient not-ready states (mergeability still computing, required checks still pending) and merge-conflict self-healing deferrals do **not** increment `final_merge_attempt_count`. On success the counter resets to `0`. After the budget is exhausted the project transitions to `status=failed` with phase label `ai:blocked` and a CRITICAL Telegram alert; it is **not** advanced to `status=complete` while the integration branch remains unmerged into the default branch. Must be a positive integer; invalid values fall back to `3`.
- Validation harness template-mode toggle: `VALIDATION_USE_TEMPLATES` (default `false`) switches `scripts/validate_process.sh` Phase 1 from freehand Codex generation to deterministic renderer mode (`scripts/render_validation_templates.py`) when set truthy (`1|true|yes|on`, case-insensitive). Template mode requires `.ai/validate.yml`, `scripts/render_validation_templates.py`, `scripts/templates/slot_manifest.schema.json`, and `workflow-templates/validation-harness/`; missing assets fail with `raw_status=harness_error` (no silent freehand fallback). Workflow bootstrap in `.github/workflows/validate.yml` now stages renderer/schema/template assets best-effort for consumer repos so opt-in can be enabled via repository variable without wrapper edits.
- Review PR-state poll interval: `REVIEW_PR_STATE_POLL_INTERVAL_SECS` (default `10`) controls `sleep` cadence for the reviewer watchdog loop in `scripts/review_run_reviewers.sh`; GitHub PR-state API checks run every 9 polls (default ~90s). Accepts integer `10..3600`, and invalid/out-of-range values emit `rate_limit_audit_fallback` warning then fail open to `10`.
- Shared Codex retry controls: `MAX_CODEX_ATTEMPTS` (default `3`) and `CODEX_RETRY_BACKOFF_BASE_SECS` (default `10`) now drive validate/workflow-log-analysis Codex retry loops. Both require positive integers and fail open to defaults with warnings when invalid.
- Phase-failure comment gate (`reserved`): `ENABLE_PHASE_FAILURE_COMMENTS` (default `true`) is a contract-defined toggle for `AI_PHASE_FAILURE_V1` emission. Current branch behavior does not consume the gate yet; validate/workflow-log-analysis still emit markers when tracking context exists.
- Label-repair sweep gates (`reserved`): `ENABLE_LABEL_REPAIR_SWEEP` (default `true`), `LABEL_REPAIR_DRY_RUN` (default `false`), and `LABEL_REPAIR_MAX_ISSUES_PER_CYCLE` (default `50`) are contract-defined knobs. Current branch behavior does not consume these gates yet; `reconcile_managed_issue_labels` runs per current-wave managed issue and applies live edits.
- Preflight F-code lint gate for embedded Python heredocs: `VALIDATE_PREFLIGHT_PYFLAKES_ENABLED` (default `true`) and `VALIDATE_PREFLIGHT_PYFLAKES_RULES` (default `F`). When enabled, `run_preflight_checks` in `scripts/validate_process.sh` extracts every quoted `python3 - <<'PY' ... PY` body under `validation/**/*.sh` and runs BOTH `pyflakes` and `ruff check --select "$VALIDATE_PREFLIGHT_PYFLAKES_RULES"` against each body. A non-zero exit from either tool fails the preflight and routes through the existing `_emit_preflight_tail` / self-heal path. Default rule set `F` covers all pyflakes-equivalent rules (F401 unused import, F811 redefinition, F821 undefined name, F823 local-before-assign, F841 unused local, etc.). Rationale: `ast.parse` catches `SyntaxError` only, so a `NameError` in a conditional branch not exercised by `tests/NN_*.sh` reaches production — the exact failure class that produced `autobet_finalize ... reason=unknown_error:NameError attempt=0` in a consumer-repo autobet flow. Missing tools are auto-installed via `python3 -m pip install --user --quiet` (with `--break-system-packages` fallback for PEP 668); if install fails the check fails open with `::warning::Preflight F-code lint fail-open: could not install ...` and proceeds, matching the fail-open convention used by `scripts/verify_integration_fingerprints.py` in §18. Invalid env values are coerced: `VALIDATE_PREFLIGHT_PYFLAKES_ENABLED` defaults to `true` on any non-`{true,false}` input; `VALIDATE_PREFLIGHT_PYFLAKES_RULES` must match `^[A-Z0-9,]+$` and defaults to `F` otherwise.
- Stall-recovery fresh-push suppression (`_check_fresh_push_guard` in `scripts/orchestrate_poll_process.sh`): when a stalled issue's phase is `ai:done` or `ai:ready-to-merge` and its linked PR's head commit was pushed within the last **30 minutes** (hardcoded constant `_FRESH_PUSH_SUPPRESS_SECS=1800`; not tunable), both stall-recovery paths (orchestrator-managed `recover_stalled_issue` and standalone `run_standalone_stall_recovery`) suppress the recovery dispatch for that cycle and emit `STALL_SKIP issue=<n> reason=fresh_push pr=<p> pushed_age_secs=<s> phase=<phase> action=<action>` (public log-prefix contract — renames are breaking per §6). Zero extra API calls: `headPushedAt` (coalesced from GraphQL `pushedDate`/`committedDate`) is embedded in the existing batched `_fetch_linked_pr_status_graphql` and `_fetch_candidate_issue_details_graphql` payloads. The guard fails open (lets stall recovery proceed as before) on phase mismatch, missing/null linked-PR entry, missing/unparseable `headPushedAt`, or negative age (clock skew). Complements — does not replace — the existing `issue_has_active_workflow` guard, covering the gap between an autofix push and the next `pull_request.synchronize` run materialising.

## 4a. Post-Codex Recovery Docs Sync

- Recovery order for implementation failures must stay documented as: (1) syntax/step failure capture, (2) in-place repair attempt layer (`MAX_POST_CODEX_REPAIR_ATTEMPTS`-capped), (3) #829 diagnose/fix-up fallback, (4) poller handling of `ai:implementation-failed` reissue/closure (capped by `MAX_IMPL_NOOP_REISSUES`).
- Current branch reality: step (2) is implemented and consumed by `implement.yml`; docs must keep the non-negative-integer validation/fallback semantics (including `0` disable mode) aligned with runtime behavior.
- Implement diagnose fix-up issues use metadata type `implement-fix-up (post-codex-validation)` and enter pipeline via `ai:clarification`; an additive `ai:implement-fix-up` label is also applied for operations visibility.
- `fix_issues[].depends_on` is additive metadata from diagnose output; `implement.yml` maps local IDs to created issue numbers via dependency-note comments. Poller state updates for implementation-failed reissues are additive (`waves[].issues[].github_issue`, `issue_number_map`) and backward-compatible with older state missing `impl_noop_count` (treated as `0`).
- Out-of-scope failures (for example missing/empty post-Codex capture artifacts) must remain on the legacy generic failure path; do not document them as part of the targeted fix-up lane.

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
- The collector (`scripts/collect_workflow_logs.py`) collects **all** workflow families (no static filter). `workflow_families` in the report is derived from observed runs.
- The collector randomly samples ~7% of successful runs for log analysis (`--success-sample-rate`, default `0.07`) using a deterministic seed. Sampled runs are tagged with `_success_sampled: true`.
- AI memory operations emit `AI_MEMORY_TELEMETRY: {JSON}` lines (stderr from `ai_memory.py`; `memory_helpers.sh` uses stdout unless stdout must remain machine-readable, then telemetry is sent to stderr). The analysis prompt instructs the LLM to produce an **AI Memory Health** section from these lines.
- `workflow-log-analysis.yml` remains `workflow_dispatch`-only and has dual execution paths: `codex_mode=true` (default) runs analyzer preprocessing (`--codex-mode`) plus `codex exec`, while `codex_mode=false` uses the legacy analyzer/batch path.
- `batch_api_disabled` input is validated whenever a non-empty value is provided, but only overrides analyzer batch behavior for `codex_mode=false`; codex-mode runs do not use the batch path.

## 13a. Comprehensive Release Callback (Poller-Owned)

- `.github/workflows/comprehensive-test-and-release.yml` has three phases only (`phase1-first-pass-test`, `phase2-collect-and-analyze-logs`, `phase3-dispatch-orchestrator`); callback handling is poller-owned, not a standalone workflow phase.
- Phase 2 dispatches `workflow-log-analysis.yml` with `codex_mode=true` and resolves the collector window from `analysis/last_collection_timestamp.txt`; invalid/missing timestamp falls back to `lookback_days_fallback`.
- Poller callback handling is label-gated: `handle_comprehensive_release_callback_if_needed` runs only while the tracking issue has `ai:comprehensive-test-pending`.
- On project status `complete`, the poller dispatches `test-and-mark-stable.yml` with `dry_run=false`, reusing validated `version_tag`/`test_repo` extracted from tracking comments when present.
- On project status `failed` or `validation-failed`, the poller sends an abort notification and does not dispatch release.
- On completion/abort callback paths, the poller writes `comprehensive_release_callback` (`handled`, `status`, `handled_at`) and removes `ai:comprehensive-test-pending` best-effort. When dispatch fails in the `complete` path, it leaves callback state/label unchanged so the poller retries on a later cycle.

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

- `scripts/validate_process.sh` attempts to self-heal prompt-wording defects in the four validation prompts (`mode-validate-{discover,generate,fix-harness,diagnose}.txt`) before burning a `MAX_VALIDATE_CYCLES` cycle. The self-heal flow is driven by `prompts/mode-validate-self-heal.txt` and `scripts/self_heal_validation.sh`. For template-mode preflight failures, `validate_process.sh` first attempts one deterministic rerender + relint recovery before terminalizing.
- The budget is `MAX_SELF_HEAL_ATTEMPTS` (default `2`) per `validate_process.sh` invocation. Self-heal re-execs do NOT increment `VALIDATION_CYCLE`.
- Self-heal is ONLY permitted to edit the four validation prompt files. Do not extend it to scripts, workflow YAML, or other prompts without a new ask-first decision.
- Render-phase (`SELF_HEAL_FAILURE_PHASE=render`) prompt self-heal remains wording-only; harness corrections in that path must stay renderer-driven and must not revert to freehand harness generation.
- On a successful healed pass, `validate_process.sh` POSTs `repository_dispatch` (event type `validation-prompt-self-heal`) to `shubhodeep1/coding-workflows` with the accumulated patches. The intake workflow `.github/workflows/validation-improvements-intake.yml` opens a **draft** PR with the `[skip ai]` title token and label `ai:needs-prompt-review`, and appends a ledger entry to `docs/validation-improvements.md`.
- Draft PRs are already skipped by `review_autofix.yml` (gate at line ~87), so the automated review/autofix/automerge pipeline cannot merge a self-heal PR without a human marking it "Ready for review". That event is handled by `workflow-templates/ai-review.yml` on the `ready_for_review` trigger.
- Admin alerts for each intake are sent via `tg_send_msg` (severity `WARNING`). See README.md section "Validation self-healing" for the full flow and the unlock procedure.
- Before changing any of the self-heal wiring or touching `prompts/mode-validate-self-heal.txt`, re-read the hard constraints section of that prompt — the allow-list of four target files, the no-rename rule, and the no-schema-change rule are all required for correctness.

---

## 17. GitHub API Call Hygiene (MANDATORY)

GitHub REST and GraphQL rate limits are a shared resource across every orchestrator and issue-processing job. Before writing **any** new `gh api`, `gh_retry`, `_safe_gh_jq`, `gh issue ... --json`, `gh pr ... --json`, `gh run list`, or direct `curl https://api.github.com/...` call, you MUST check whether the data can be obtained from an existing call in the same code path and merged or batched with it.

Rules:

- **Check first, add second.** Search the surrounding function and file for existing `gh` invocations hitting the same issue/PR/repo scope. If one exists, extend it (add a JSON field, add a GraphQL alias, reuse its cached result) instead of creating a new call.
- **Prefer batched GraphQL over per-item REST.** For N-item data needs (issues, PRs, comments, labels, timeline events), use aliased GraphQL queries. Canonical examples in this repo: `_fetch_candidate_issue_details_graphql` and `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`. Both return a dict keyed by issue number so the caller can drop the result into a cycle-local cache.
- **Cycle-local caches are first-class.** Orchestrator loops that need the same data for many iterations MUST prefetch once into a shell/file cache (`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`, `_candidate_details_json`) and have the inner loop read from the cache. Adding a per-iteration `gh api` call inside such a loop is a review-blocker.
- **Fail open on cache miss.** A cache/prefetch failure must never block the caller — fall back to the smallest safe legacy call, not a tight retry loop.
- **Document the batching contract.** When you add a batched helper, spell out in the function docstring the input shape, output shape, number of API calls issued, and fail-open behaviour so future callers can reuse it without re-reading the implementation.
- **Inventory and reporting signals are contractual.** Keep stable log prefixes that feed workflow-log-analysis/API-hygiene reporting (`LABEL_REPAIR`, `LABEL_REPAIR_DIFF`, `AUTOFIX_PEER_CHECK`, `AUTOFIX_DISPATCH_SKIPPED`, `AUTOFIX_DISPATCH_ISSUED`, `AI_PHASE_FAILURE_V1`). Renames are breaking unless an alongside-old shim is documented and shipped.
- **Label-repair contradiction policy (current branch).** Active poller loop uses `reconcile_managed_issue_labels` for current-wave managed issues and logs `LABEL_REPAIR*` diagnostics. The richer contradiction-evidence helpers in `scripts/orchestrate_lib.py` (`parse_phase_failure_markers`, `choose_most_advanced_conclusive_evidence`, `resolve_label_repair_evidence`) are contract/reserved and not yet wired into poller reconciliation.

If you need a new data shape that truly cannot be satisfied by any existing call, add a comment above the new invocation explaining which existing calls you audited and why they were insufficient.

---

## 18. Internal Wrapper Pin Policy

- The `.github/workflows/internal-*.yml` wrappers in this repo MUST pin
  `uses:` to `shubhodeep1/coding-workflows/.github/workflows/<wf>.yml@main`.
  Do NOT revert them to local refs (`./.github/workflows/<wf>.yml`) and do
  NOT flip them to `@stable`. The `@main` pin is required for two reasons:
  (a) it makes orchestrator runs immune to stale reusable-workflow copies
  on feature branches (which previously caused hangs that were hard to
  fix mid-run), and (b) it still lets `test-and-mark-stable.yml`'s E2E
  smoke test validate main HEAD — because `issues:[opened]` events fire
  the default-branch wrapper, which then fetches the reusable body from
  `@main` (= main HEAD = the candidate about to be tagged).
- Consumer templates under `workflow-templates/ai-*.yml` MUST stay pinned
  to `@stable`. Do not unify the two pin targets.
- `ai-update-workflows.yml` must NOT be installed into `.github/workflows/`
  in this repo. The self-updater in `update_workflows.yml` copies files
  from `workflow-templates/*.yml` into `.github/workflows/` keyed by exact
  filename, so the current `internal-*.yml` filenames are not directly
  overwritten. The hazard is different: on first run the self-updater
  would **create** new `ai-*.yml` wrappers pinned `@stable` (because
  those filenames are absent today), which would then auto-fire on the
  same issue/PR events as the `internal-*.yml` wrappers and cause
  duplicate runs and racing state writes. Keeping the self-updater
  uninstalled prevents that creation path entirely. Do not rename
  `internal-*.yml` to `ai-*.yml` without first removing this risk.
- PR-time dogfood gate: `ci.yml` runs `yamllint` and `actionlint` across
  `.github/workflows/*.yml` and `workflow-templates/*.yml` on
  `pull_request` against `main`. Any change that breaks YAML or GitHub
  Actions schema must be caught here before landing on `main`, because a
  broken merge to `main` immediately breaks all in-flight orchestrator
  runs (accepted trade-off for fast recovery).
- Recovery procedure for a broken `main` reusable: push the fix directly
  to `main` (or merge a hotfix PR). The next wrapper invocation picks it
  up immediately. Only run `test-and-mark-stable.yml` when promoting the
  fix to the `@stable` channel for consumer repos — it is not on the
  critical path for recovering this repo's own runtime.

## 19. Workflow Checkout Integration-Ref Contract

- Orchestrator-managed issue-phase workflows that checkout repository state from issue/issue_comment context (`.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/orchestrate_clarify_respond.yml`, `.github/workflows/implement.yml`) plus validation runs keyed by `inputs.tracking_issue` (`.github/workflows/validate.yml`) MUST resolve integration branch metadata before `actions/checkout@v5`.
- Required checkout wiring in those workflows:
  - pre-checkout step `- name: Resolve integration ref` with `id: refctx`
  - checkout ref binding: `ref: ${{ steps.refctx.outputs.ref || github.event.repository.default_branch }}`
  - post-checkout logging of resolved ref + `git rev-parse HEAD` + `git symbolic-ref --short HEAD` (with detached fallback)
- Resolver behavior must fail open: missing metadata, invalid format, missing branch, or GH API failure must emit warning/notice and leave `steps.refctx.outputs.ref` empty (checkout falls back to default branch).
- `orchestrate_poll.yml` is an explicit exception: one run can process multiple tracking issues, so a single integration ref cannot be chosen safely for a shared checkout.
- Regression guard: `tests/test_workflow_checkout_integration_ref_audit.py` scans all `.github/workflows/*.yml` checkout@v5 usages and fails unless each file is either in the required-resolver set above or in an explicit allow-list with rationale.

---

## 18. Orchestrator Integration-Sync Auto-Heal Hardening

When a sub-issue PR merges into an orchestrator integration branch (head matches `orchestrator/project-*`), the poller captures intent fingerprints from the merged diff and persists them under the new top-level state field `merged_issue_fingerprints` (object keyed by GitHub issue number). Each entry stores `must_contain` and `must_not_contain` regex pattern lists derived from added/removed lines in the PR diff.

When a `main → integration_branch` sync conflict subsequently triggers `heal_integration_branch_conflict`:

- The resolver dispatch uses `prompts/integration-sync-conflict-resolver.txt` (rendered by `.github/workflows/review_autofix.yml` when the head ref matches `orchestrator/project-*`) instead of the generic `prompts/conflict-resolver.txt`. The integration template injects the tracking-issue title/body, the merged sub-issues list, and the full `merged_issue_fingerprints` JSON, and instructs the model to synthesise rather than pick a side when both sides of a hunk carry merged sub-issue intent.
- After the codex resolver writes the working tree but **before** the `[ai-merge-resolve]` commit lands, the resolver step calls `scripts/verify_integration_fingerprints.py` against the captured fingerprints. A `must_contain` regex that no longer matches, or a `must_not_contain` regex that reappears, hard-fails the resolver step with `::error::` annotations. The merge state is left intact so the next poll tick re-enters healing and escalates to the integration judge (per `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES=1` default).
- The retry budget is **branch-aware** in `heal_integration_branch_conflict`: orchestrator integration branches honour the tighter `INTEGRATION_SYNC_CONFLICT_MAX_RETRIES` (default 1), all other dispatch sites honour the historical `INTEGRATION_CONFLICT_MAX_RETRIES` (default 3).

Operational rules:

- The fingerprint capture helper (`capture_intent_fingerprints_for_merged_subissue`) is **going-forward only** — sub-issues merged before this hardening landed have no fingerprints and are silently skipped by the verifier.
- `verify_integration_fingerprints.py` lives in `OPTIONAL_BOOTSTRAP_SCRIPTS` so older consumer-repo `script_ref` pins bootstrap cleanly with a fail-open warning rather than a hard error. Do **not** promote it to `REQUIRED_BOOTSTRAP_SCRIPTS` until the next stable channel cut, otherwise consumer repos pinned to the prior `@stable` will break on bootstrap.
- The verifier exits 0 on success, 1 on hard violation (resolver step aborts, no commit), 2 on plumbing failures (file missing or unparseable JSON — fail-open warn). Do not change those exit codes — `review_autofix.yml` keys its `case` on them.
- New state field `merged_issue_fingerprints` must be seeded by `ensure_integration_conflict_state_fields` on every poll tick so jq arithmetic over it is safe.
- All renames of the new env vars and new state field are **breaking changes** per CLAUDE.md §6 (Naming Immutability) and require an explicit alongside-old shim.

---

## 20. Autofix Retrigger Dedup

`review_autofix.yml` has two `workflow_dispatch` retrigger steps that fire after a push — the post-commit/merge-resolve retrigger and the editor-changes-lost retrigger. Both used to dispatch unconditionally, which collided with the `pull_request.synchronize` event produced by the same push. With `cancel-in-progress: false` those redundant dispatches can still produce queued runs (and extra API/UI noise), so the retrigger dedup guard now probes for an in-flight peer first and skips dispatch when one already exists.

Contract:

- Each retrigger step waits `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` (default `8`; invalid/non-numeric or `> 60` values reset to `8`) for the synchronize run to materialise, then calls `autofix_retrigger_has_inflight_peer "${PR_NUMBER}" "${TARGET_BRANCH}" "${GITHUB_RUN_ID}"` in `scripts/gh_helpers.sh`.
- The helper issues exactly **1** `gh api GET /repos/{repo}/actions/runs?branch=...&per_page=30` call (wrapped in `gh_retry`) per invocation and filters in `jq` for queued/in_progress runs on the review-workflow paths (`review_autofix.yml`, `internal-review.yml`, `ai-review.yml`) excluding the current run ID. Returns 0 on peer found, 1 otherwise.
- The helper **fails open**: any API or empty-response error returns 1 so the caller falls through to the original unconditional dispatch. A missing `autofix_retrigger_has_inflight_peer` symbol (old bootstrap) also falls through.
- When a peer is found the retrigger emits `AUTOFIX_DISPATCH_SKIPPED reason=<reason> pr=<n> current_run=<r> source=<post_commit|editor_changes_lost>_retrigger` and exits 0 without dispatching. When no peer is found it emits `AUTOFIX_DISPATCH_ISSUED reason=no_peer_detected ...` and proceeds with the existing dispatch chain (direct `review_autofix.yml` → `ai-review.yml` / `internal-review.yml` fallback).
- Every probe emits `AUTOFIX_PEER_CHECK pr=... branch=... current_run=... peer_count=... peer_run=... peer_path=...` so Actions log analysis can measure collision rates over time. Probe failures emit `AUTOFIX_PEER_QUERY_FAILED pr=... branch=... reason=<missing_inputs|api_error|empty_response|jq_error>` on stderr.

Operational rules:

- Renames of `AUTOFIX_RETRIGGER_PEER_WAIT_SECS`, the helper, or the log prefixes (`AUTOFIX_PEER_CHECK`, `AUTOFIX_DISPATCH_SKIPPED`, `AUTOFIX_DISPATCH_ISSUED`, `AUTOFIX_PEER_QUERY_FAILED`) are breaking changes per CLAUDE.md §6 — downstream log analysis (`scripts/analyze_workflow_logs.py`) and any consumer dashboards pivot on those literal prefixes.
- Per CLAUDE.md §15 audit: the prior retrigger path issued no `gh` call, so there was no existing invocation to extend. The single added list-runs call replaces a guaranteed-wasted `gh workflow run` dispatch on the collision path; net API cost is negative when a peer is found and neutral otherwise. The helper is **not** a candidate for cycle-local caching because it is called at most twice per run and the in-flight run set is mutable between calls.
- The helper is bootstrap-safe: `gh_helpers.sh` is already in `REQUIRED_BOOTSTRAP_SCRIPTS`, so the symbol is available from the first step after bootstrap. Do not move `gh_helpers.sh` to the optional list.

---

## FINAL REMINDER

If uncertainty exists: **ASK (multiple-choice). DO NOT EXECUTE.**

Accuracy > speed. Safety > convenience. Backward compatibility is mandatory.
