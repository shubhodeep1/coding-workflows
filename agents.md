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
- No-op ancestor-chain cap (belt-and-braces for `MAX_IMPL_NOOP_REISSUES`): the shell helper `count_noop_ancestors` in `scripts/orchestrate_poll_process.sh` walks the `Re-issued from #N` chain up to `MAX_IMPL_NOOP_REISSUES` hops and counts ancestors whose issue-comments contain the implement.yml no-op warning signature `produced no repository changes`. It is wired into **all three** orchestrator re-issue paths: (1) `execute_stall_recovery_action close_and_reissue` (main stall recovery), (2) `run_standalone_stall_recovery close_and_reissue` (standalone stall recovery), (3) the `IF_MODE=no-op-implementation` branch of the `ai:implementation-failed` sweep that consumes `get_impl_noop_count`. When the ancestor count ≥ cap the issue is closed with `ai:closed` and the wave-completion judge verifies on the integration branch instead of spawning another re-issue. Fail-open: on any `gh api`/`_safe_gh_jq`/parse error the helper returns `0` and callers fall through to the legacy re-issue flow. Rationale — this catches the failure mode where the state-based counter is stale (tracking-issue state comment truncated, or the wave iterator never refreshed `get_impl_noop_count`), which caused tracking issue #1292 to spawn 30+ duplicate sub-issues for `local_id=validation-render-self-heal` in ~5 hours. API cost: up to `2 * MAX_IMPL_NOOP_REISSUES` calls per invocation (one `GET /issues/{n}` + one `GET /issues/{n}/comments` per hop, stops early on first non-no-op ancestor).
- Ancestor-chain cap in `implement.yml` (`IMPL_NOOP_ANCESTRY_THRESHOLD`, default `2`): the "Handle no-op implementation" step in `.github/workflows/implement.yml` performs the same ancestor-chain walk **issue-local** before falling through to `ai:implementation-failed`, so even a caller repo that cannot see orchestrator state (standalone dispatch) still benefits from the cap. On threshold match it closes the issue with `ai:closed` and an explanatory comment; otherwise it falls through to the legacy `ai:implementation-failed` labeling that the poller consumes. The threshold is configurable per-repo via `vars.IMPL_NOOP_ANCESTRY_THRESHOLD`; invalid values fall back to `2`.
- Success-no-op short-circuit in `implement.yml` (Guard 0 in the "Handle no-op implementation" step): the "Run Codex implementation" step snapshots the worktree with `git status --porcelain -uall` into `${RUNTIME_DIR}/codex_pre_baseline.txt` BEFORE the retry loop (so runtime support checkouts — `.codex-workflow-src`, `.codex-workflow-src-main`, `.serena`, `ai-memory/schemas`, … — do not register as Codex-produced changes). Both the retry-nudge check and the success-detection check compute `git status --porcelain -uall | grep -vxFf "${CODEX_PRE_BASELINE}"` so only Codex-caused deltas count. When that delta is empty AND the Codex stdout matches `/no file changes were made|nothing to change|already (aligned|implemented|up[- ]to[- ]date|done|exists|present|complete)|no changes needed/i`, the step touches `${RUNTIME_DIR}/codex_success_noop.flag` and breaks the loop with `implement_succeeded=true`. Guard 0 runs at the top of the "Handle no-op implementation" step: on flag-file presence it closes the issue with `ai:closed` + an ✅ "Already implemented — closing without changes" comment and exits with `0`, bypassing both Guard 1 (pathspec hard-fail to `ai:needs-human`) and Guard 2 (ancestor-chain `ai:closed` cap). Rationale — without this, a Codex success-no-op on a consumer-repo run leaves `.codex-workflow-src*` as the only surviving `remaining_changes`, which would otherwise falsely trip Guard 1's pathspec regex and mislabel the issue `ai:needs-human`. Observed failure mode: issue #141 spawned a cascade of `ai:implementation-failed` re-issues after Codex correctly reported "No file changes were made" (the allowlist drift the plan addressed had already been fixed by a sibling sub-task). Fail-open: flag creation failure or missing `RUNTIME_DIR` causes the check to skip and fall through to Guards 1/2 as before.
- Pathspec hard-fail escalation (`.codex-workflow-src*` filter): the same "Handle no-op implementation" step inspects `steps.commit_changes.outputs.remaining_changes` (the unstaged-paths listing) and, if it contains `.codex-workflow-src` or `.codex-workflow-src-main`, labels the issue `ai:needs-human`, posts a CRITICAL Telegram alert (via inline `curl https://api.telegram.org/bot${TG_BOT_SECRET}/sendMessage`, not `tg_helpers.sh` — the support checkout may have been stripped in caller-repo runs), and `exit 1`s the job. This blocks the orchestrator's re-issue loop on the class of failure where Codex writes into the runtime-fetched support checkout and the commit pathspec exclusions (around `add_u_excludes` / `add_o_excludes` in `implement.yml`) silently strip those changes — observed as the root cause of tracking issue #1292's runaway loop. Requires human review of the pathspec exclusions before automation can resume; there is no auto-retry.
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
- Review autofix `run:` block extractions (GitHub Actions 21,000-char expression-template limit): three formerly-inline steps in `.github/workflows/review_autofix.yml` are now delegated to support scripts so each step's remaining `run:` block stays well under the per-step expression cap (a single over-limit block silently breaks `internal-review.yml`'s ability to parse the reusable workflow). Contracts: (1) `scripts/review_commit_changes.sh` — invoked from the "Commit changes" step with `env: GH_PAT: ${{ secrets.GH_PAT }}`; reads `CAN_PUSH`, `IS_WORKFLOW_SOURCE_REPO`, `ALLOW_WORKFLOW_EDITS`, `COMMITTED_FILES_FILE`, `RUNTIME_DIR`, `PRE_EDITOR_STATE_FILE`, `PRE_EDITOR_DIFF_BASELINE_FILE`, `LAST_RUN_DIFF_FILE`, `EDITOR_SUMMARY_FILE`, `REVIEW_LEDGER_PATH`; writes `DID_COMMIT`/`LEDGER_ONLY_COMMIT` to `$GITHUB_ENV` and `did_commit`/`ledger_only_commit` to `$GITHUB_OUTPUT`; no-ops on `CAN_PUSH!=true`. (2) `scripts/review_conflict_prepare.sh` — "Prepare merge-conflict resolver prompt and pre-snapshot"; renders `${CONFLICT_RESOLVER_PROMPT_FILE}` from `${SUPPORT_PROMPTS_DIR}/conflict-resolver.txt` (or integration-sync variant when on `orchestrator/project-*`), emits `pre_resolver_state.tsv` / `conflicted_paths.txt` / `resolver_unmerged_allowlist.txt` under `${RUNTIME_DIR}`, and exports `INTEGRATION_FINGERPRINTS_FILE`, `INTEGRATION_BRANCH_NAME`, `INTEGRATION_TRACKING_NUM`, `IS_INTEGRATION_SYNC`; clears `MERGE_CONFLICT` and exits 0 when the merge replay produces no unmerged paths. (3) `scripts/review_conflict_resolve.sh` — "Run Codex resolver, validate, stage, commit"; invoked with `env: GH_PAT: ${{ secrets.GH_PAT }}`; consumes the prepare-step artefacts, runs `codex exec` with up-to-3 retries, enforces the allowlist + `scripts/check_resolver_diff.sh` + (integration) `scripts/verify_integration_fingerprints.py` guards, creates a single `[ai-merge-resolve]` commit (push deferred), and writes `CONFLICT_RESOLVED`. All three scripts are listed in `REQUIRED_BOOTSTRAP_SCRIPTS` in `.github/workflows/review_autofix.yml` ("Stage workflow support files" step) — adding/removing an entry is a breaking change per §6 and must be mirrored in both source checkouts (`.codex-workflow-src` / `.codex-workflow-src-main`). Any future edit that materially grows one of these scripts must also re-audit `review_autofix.yml` for other `run:` blocks creeping back toward the 21,000-char limit.
- Implement post-Codex diagnose extraction (same 21,000-char limit): the `Diagnose post-Codex failure and create fix-up issues` step in `.github/workflows/implement.yml` is delegated to `scripts/implement_diagnose_post_codex_failure.sh`. The step now carries `env: GH_TOKEN: ${{ secrets.GH_PAT }}`, `OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}`, `JOB_STATUS: ${{ job.status }}`, `DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}`; the script additionally reads `ISSUE_NUMBER`, `RUNTIME_DIR`, `MODEL_EDITOR`, `ISSUE_META_FILE`, `ISSUE_BODY_FILE`, `PR_BASE_BRANCH`, `IMPLEMENT_DIAGNOSE_PROMPT_FILE`, `IMPLEMENT_DIAGNOSE_OUTPUT_FILE`, `IMPLEMENT_DIAGNOSE_LOG_FILE`, `IMPLEMENT_DIAGNOSE_RESULT_FILE` and the auto-set `GITHUB_REPOSITORY` / `GITHUB_RUN_ID` / `GITHUB_SERVER_URL`. It writes `handled=true|false` to `$GITHUB_OUTPUT` and no-ops (exit 0) when `RUNTIME_DIR` is unset, the captured validation-errors file is missing, or the source issue already carries `ai:implementation-failed`. The script must be listed in `implement.yml`'s "Stage workflow support files" staging loop — adding/removing the filename is a breaking change per §6. Any future edit that materially grows this script must re-audit `implement.yml` for other `run:` blocks creeping back toward the 21,000-char limit (next-largest is `Run Codex implementation` at ~15.4 KB).
- Stall-judge diagnostics builder hardening (`invoke_stall_judge` in `scripts/orchestrate_poll_process.sh`): the `recent_tracking_comments` slice now (1) filters out `ORCHESTRATOR_STATE_V1` snapshot comments before taking the last-8 window — those are ~57KB state dumps, not judge-usable phase/recovery narrative — and (2) truncates each surviving body to 2000 characters (appending `…[truncated]`). Without both caps, tracking issues whose last-8 comments included state snapshots produced a ~260KB `recent_comments` JSON blob; passing that to the final diagnostics `jq -cn` via `--argjson recent_comments "${recent_comments}"` exceeded Linux `MAX_ARG_STRLEN` (128KB per argv entry), returned `E2BIG` (surfaced as `/bin/bash: /usr/bin/jq: File name too long`, exit 126), and silently produced `diagnostics=""` — the judge then received an empty JSON code block, correctly complained ("Stall diagnostics payload is empty…"), and the safety normalizer downgraded its `escalate_human` output to the phase's fallback action. A defensive post-build guard now validates that `diagnostics` is a non-empty JSON object; on any failure it emits `::warning::Stall judge diagnostics builder failed for issue #<n>…` and substitutes a minimal fallback payload (`issue_number`, `local_id`, `phase`, `stall_minutes`, `recovery_count`, `linked_pr.{number,state,head_ref,base_ref}`, `diagnostics_build_failed: true`) so the judge still sees the essential decision fields and the failure is visible in the workflow log instead of hidden behind the judge's prose assessment. Observed in prod: run 24761340584 for issue #1504 / PR #1505 (tracking #1479); identical pattern hit #1503 in the same cycle. Fail-open: any builder hiccup still produces a non-empty, valid JSON payload rather than blanking the judge blind.
- Stall-recovery merge-conflict pre-dispatch override (`_check_open_pr_conflict_guard` in `scripts/orchestrate_poll_process.sh`): when `run_standalone_stall_recovery` is about to dispatch `retrigger_review` (phase `ai:done`) AND the cached linked-PR entry shows `state=OPEN` with `mergeable ∈ {CONFLICTING,false}` OR `mergeStateStatus/mergeable_state == DIRTY`, the action is rerouted to `_dispatch_review_for_conflicts` instead of pushing an empty commit. Zero extra API calls on cache hit: `_fetch_candidate_issue_details_graphql` is extended to also emit `head_ref`, `mergeable`, and `merge_state_status` on the cross-referenced PR node in `_candidate_details_json`. Cache-miss **or** cache-reports-UNKNOWN triggers a REST `GET /pulls/{n}` retry loop of up to 5 attempts with sleeps 5/10/15/20 s between retries (50 s worst case), matching GitHub's REST docs — a push kicks off async mergeability recomputation so a single GET can return `null`/`unknown`, and the first REST call itself re-kicks the computation. The loop breaks early either when `mergeable_state` settles to `DIRTY` (conflict known even with `mergeable=null`) or when `mergeable ∈ {true,false}` with `mergeable_state ≠ unknown`. On all-attempts-still-unknown the guard fails open. The loop's final PR JSON is cached iteration-locally in `_STD_ITER_PR_JSON_CACHED` and reused by the downstream legacy `retrigger_review` case, saving one redundant `gh api` call in the fail-open path (CLAUDE.md §15 API hygiene). On a hit the poller emits `STALL_RECOVERY issue=<n> reason=open_pr_merge_conflict pr=<p> phase=<phase> action=dispatch_conflict_resolver override_from=retrigger_review` (public log-prefix contract — renames are breaking per §6) and `continue`s the loop **without** incrementing `stall_recovery_count` — conflict resolution runs on a separate budget and does not burn the retrigger-style recovery allowance. Duplicate same-cycle dispatches produce `STALL_SKIP reason=open_pr_merge_conflict_dispatch_skipped`; dispatch failures produce `STALL_RECOVERY reason=open_pr_merge_conflict_dispatch_failed`. A belt-and-braces check in `execute_stall_recovery_action retrigger_review` performs the same mergeable/mergeable_state probe (reusing its head_ref PR fetch), recursively dispatches `resolve_merge_conflict`, and forces `STALL_RECOVERY_SHOULD_INCREMENT="false"` to keep the override budget-neutral even when invoked from the managed path. Fails open: if neither cache nor REST confirms a conflict, the legacy empty-commit push fires as before — the guard can only redirect an action that would otherwise have fired, never cause one.

## 4a. Post-Codex Recovery Docs Sync

- Recovery order for implementation failures must stay documented as: (1) syntax/step failure capture, (2) in-place repair attempt layer (`MAX_POST_CODEX_REPAIR_ATTEMPTS`-capped), (3) #829 diagnose/fix-up fallback, (4) poller handling of `ai:implementation-failed` reissue/closure (capped by `MAX_IMPL_NOOP_REISSUES` state-counter AND the `count_noop_ancestors` ancestor-chain belt-and-braces cap — either signal trips closure). A separate hard-fail route for pathspec-stripped changes (Codex edits inside `.codex-workflow-src*`) escalates directly to `ai:needs-human` without going through any re-issue path, per the CRITICAL alert documented in §4.
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

## 13a. Workflow Log Analysis And Improvement Workflow

- `.github/workflows/comprehensive-test-and-release.yml` (workflow name: **Workflow Log Analysis And Improvement**) has two phases: `phase2-collect-and-analyze-logs` and `phase3-dispatch-orchestrator`. Job IDs retain the `phase2-*` / `phase3-*` names (no `phase1-*` job) for backward compatibility with external references.
- Phase 2 dispatches `workflow-log-analysis.yml` with `codex_mode=true` and resolves the collector window from `analysis/last_collection_timestamp.txt`; invalid/missing timestamp falls back to `lookback_days_fallback`.
- Phase 3 dispatches `internal-orchestrate.yml` with a project description that links to the analysis report and emits the resolved orchestrator tracking issue number as a job output. Phase 3 does NOT apply `ai:comprehensive-test-pending` or post a `COMPREHENSIVE_RELEASE_METADATA_V1` comment; release dispatch from this workflow has been removed.
- Release marking via `test-and-mark-stable.yml` remains available as a standalone workflow; it is no longer invoked from this workflow. The poller's `handle_comprehensive_release_callback_if_needed` code path and the `ai:comprehensive-test-pending` label definition are retained in `scripts/orchestrate_poll_process.sh` and `scripts/label_helpers.sh` but are currently inert because no workflow applies the gating label.

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

### 20.1 Self-Triggered Autofix Skip

Peer-dedup (§20) only collapses **parallel** runs — it does not address the serial "every autofix commit triggers a fresh `pull_request.synchronize` event that re-runs the full reviewer/editor cycle" pattern that roughly doubles LLM spend per fix round (one verification pass producing a `not-edited` comment, immediately followed by the next autofix iteration producing an `edited` comment). The self-triggered autofix skip collapses those serial verification passes at the gate.

Contract:

- Repository variable `AUTOFIX_SKIP_SELF_TRIGGERED` (default `true`; set to literal string `false` to opt out) is wired into two evaluation points in `review_autofix.yml`:
  1. **Gate job** (`jobs.gate.steps.evaluate`) — runs **before** every downstream job. When the event is `pull_request.synchronize` and the flag is not `false`, the step issues exactly **1** `gh api repos/<repo>/commits/<PR_HEAD_SHA> --jq '[(.commit.message // "" | split("\n")[0]), (.author.login // ""), (.committer.login // "")] | @tsv'` call (no retry wrapper — the probe is best-effort and fails open). The `split("\n")[0]` extracts the subject line only; `// ""` defaults ensure missing fields produce an empty string rather than `null` / jq error. If the subject begins with `[ai-autofix]` **and** at least one of `.author.login` / `.committer.login` equals `AUTOFIX_BOT_LOGIN` (default `codex`), the gate sets `should_run=false` and writes `skip_reason=self_triggered_autofix` to `$GITHUB_OUTPUT`. Critically, the gate reads `.author.login` / `.committer.login` — GitHub-attributed identity resolved server-side from the push credentials — **not** `.commit.author.email`, which is user-controlled `git config` metadata and therefore spoofable. When the subject matches but neither login equals the configured bot, the gate logs `AUTOFIX_GATE_NO_SKIP_IDENTITY ...` and leaves `SHOULD_RUN=true`. All other events (`workflow_dispatch`, `opened`, `reopened`, `ready_for_review`, `closed`) bypass the probe unconditionally.
  2. **Post-commit `workflow_dispatch` retrigger step** — mirrors the gate filter but guards on locally-available signals instead of re-querying the HEAD commit: skip only when `DID_COMMIT=true` **AND** `CONFLICT_RESOLVED!=true`. This preserves the post-conflict-resolution verification pass for `[ai-merge-resolve]` commits (a resolver-produced HEAD must still get reviewed). The guard runs **before** the existing `autofix_retrigger_has_inflight_peer` peer-dedup.
- **Identity guard is load-bearing and must use GitHub-attributed identity**: the gate compares `.author.login` / `.committer.login` (resolved by GitHub from the push credentials) against `AUTOFIX_BOT_LOGIN` so that a human author cannot suppress review by crafting a commit with `git config user.email=codex@users.noreply.github.com` and an `[ai-autofix]`-prefixed subject. Do not replace the `login` comparison with `.commit.author.email` or `.commit.author.name` — those come from local `git config` and are user-controlled. Treat both logins being empty (unauthenticated mirror push, or an email not linked to any GitHub account) as "fail open, run review".
- **Event guard is load-bearing**: `workflow_dispatch` (including orchestrator stall-cron re-kicks) and `opened` / `reopened` / `ready_for_review` events must always run the full cycle — they represent explicit human/system intent to re-verify. The gate's `EVENT_NAME == 'pull_request' && EVENT_ACTION == 'synchronize'` condition is the only path that can skip.
- **Fail-open on probe error**: when the `gh api commits/<sha>` call fails, the gate emits `AUTOFIX_GATE_SKIP_QUERY_FAILED pr=<n> head_sha=<sha> reason=api_error` and leaves `SHOULD_RUN=true`. Never convert this to a hard fail; a GitHub API blip must not cause the review to be silently dropped.

Log prefix contract (stable — renames are breaking changes per CLAUDE.md §6):

- `AUTOFIX_GATE_SKIP reason=self_triggered_autofix pr=<n> head_sha=<sha> head_prefix=[ai-autofix] author_login=<login> committer_login=<login> bot_login=<expected>` — gate skip decision. The `author_login` / `committer_login` / `bot_login` fields are GitHub-attributed identity used by the identity guard (see §20.1 contract).
- `AUTOFIX_GATE_NO_SKIP_IDENTITY pr=<n> head_sha=<sha> head_prefix=[ai-autofix] author_login=<login> committer_login=<login> bot_login=<expected>` — subject matched `[ai-autofix]` but neither login equalled `AUTOFIX_BOT_LOGIN`; gate falls through and runs review (spoofed-subject defence, audit handle).
- `AUTOFIX_GATE_SKIP_QUERY_FAILED pr=<n> head_sha=<sha> reason=api_error` — probe fail-open fallthrough.
- `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix pr=<n> current_run=<r> source=post_commit_retrigger` — new `reason=` value on the existing dispatch-skipped prefix. Downstream log analysis should treat `self_triggered_autofix` as a distinct bucket from `peer_inflight` / `sync_event_inflight`.

Safety net:

- If a required verification pass is incorrectly skipped (e.g. the author guard misfires or an edge case slips through), the orchestrator stall cron (`internal-orchestrate-poll.yml`, cron `*/30 * * * *`) detects the stalled `ai:done` phase and re-dispatches `review_autofix.yml` via `workflow_dispatch` — which bypasses the skip. Worst-case recovery window is ~30 min. Do **not** lengthen the stall cron cadence beyond this value without re-evaluating the skip contract.

API cost audit (CLAUDE.md §15):

- The gate adds **at most 1** `gh api commits/<sha>` call per `pull_request.synchronize` run, and only when the flag is enabled. The saved cost when the probe matches is the full downstream `review`/`editor` job chain — 5 reviewer codex-cli invocations + 1 consensus summariser + 1 editor invocation = 7 LLM calls, plus the existing per-step `gh` overhead. Net API cost is strongly negative on the hit path.
- The post-commit guard adds **zero** new `gh` calls (it reads pre-existing shell vars `DID_COMMIT` / `CONFLICT_RESOLVED`). It saves a `gh workflow run` dispatch on the hit path.
- Neither path is a candidate for cycle-local caching because each evaluation is scoped to a single run.

Operational rules:

- Renames of `AUTOFIX_SKIP_SELF_TRIGGERED`, `AUTOFIX_BOT_LOGIN`, the `skip_reason` output key, `self_triggered_autofix` as a reason literal, or any of the log prefixes above are **breaking changes** per CLAUDE.md §6. Add alongside the old name and continue emitting the old log line in parallel for at least one stable-channel cycle before removing.
- The gate evaluation step must remain the **first** decision point in the review pipeline so consumer-repo wrappers inherit the skip for free via the reusable-workflow call. Do not move the skip logic downstream into per-reviewer jobs.
- Do **not** extend the skip to `[ai-merge-resolve]` commits without also adding a post-resolution review path — the current design relies on the mirror guard's `CONFLICT_RESOLVED!=true` clause to ensure resolver commits still get reviewed exactly once before merge.

### 20.2 Mid-Run External-Push Gates

§20.1 catches the *post-run* self-triggered event (autofix push → synchronize → new run). It does **not** catch the *mid-run* case where a human (or any non-autofix actor) pushes to the PR branch **while** the reviewer/editor cycle is mid-flight. The `codex-agent` job runs ~15-30 min; any push to `TARGET_BRANCH` during that window leaves the editor operating against a stale base, and the deferred `Push all pending commits` step hits one of two failure modes at the end:

1. **Text-disjoint concurrent push** — the merge-retry block in `.github/workflows/review_autofix.yml`'s "Push all pending commits" step fetches origin, does a no-ff merge, and retries the push. Recovers automatically.
2. **Overlapping concurrent push** — the fetch+merge hits a real content conflict. The retry block intentionally hard-fails (`##[error]git merge origin/${TARGET_BRANCH} failed — merge would introduce new conflicts. Aborting merge and failing the job; refusing to silently resurrect conflict markers.`) rather than silently clobber either side. The whole iteration's work is lost and a `phase_failed` ledger entry is written.

Contract:

- Two evaluation points in `jobs.codex-agent`, both backed by the shared helper `scripts/check_external_branch_advance.sh`:
  1. **Pre-editor gate** (`steps.pre_editor_stale_base_gate`) — runs after "Record reviewer consensus candidate in memory", before "Install project dependencies" + "Switch reasoning effort for editor" + "Apply fixes with editor model". If the remote tip of `TARGET_BRANCH` advanced past `INITIAL_HEAD_SHA` with a non-autofix commit, the step sets `AUTOFIX_STALE_BASE_SKIP=true` in `$GITHUB_ENV`. Every downstream editor/commit/push/dispatch/clean-review step gates on `env.AUTOFIX_STALE_BASE_SKIP != 'true'` and silently skips — no commit, no push, no retrigger, no `ai:ready-to-merge` label, no Telegram success. The synchronize event from the advancing push is responsible for kicking off a fresh run against the new tip.
  2. **Pre-push gate** (inline at the top of `Push all pending commits`'s `run:` block) — runs only when the editor actually produced a commit (`DID_COMMIT == 'true'` or `CONFLICT_RESOLVED == 'true'`). Same helper, same detection, same `AUTOFIX_STALE_BASE_SKIP=true` signal. Catches the narrower window where the external push lands **during** the editor run (after the pre-editor gate passed). Soft-exits before attempting `git push`, which means the expensive editor call is lost but no `##[error]` / `phase_failed` is emitted.

- **`INITIAL_HEAD_SHA` is load-bearing**: captured by `Checkout PR head branch` via `git rev-parse HEAD` **after** the hard-reset to `refs/remotes/origin/${HEAD_REF}`, not from `github.event.pull_request.head.sha`. The hard-reset is what the reviewers actually see, so that is the SHA both gates must compare against. Do not replace with the webhook-delivered `head.sha` — the two can differ when another run (or human) advanced the branch between workflow trigger and the `Checkout PR head branch` step.

- **Shared helper contract** (`scripts/check_external_branch_advance.sh`): stdout-only protocol — emits exactly one of `ADVANCE=none`, `ADVANCE=self_only`, `ADVANCE=external`, `ADVANCE=unknown` to stdout; stderr is free-form diagnostic logging; exit code is always 0 in production. Required env: `TARGET_BRANCH`, `LOCAL_HEAD_SHA`, `GH_TOKEN`. Optional: `AUTOFIX_BOT_LOGIN` (default `codex`), `GITHUB_REPOSITORY` (auto-derived from `git remote` when unset). The helper does subject-line screening first (zero-API; any commit whose subject does NOT start with `[ai-autofix]`/`[ai-merge-resolve]` is unambiguously external) and only issues `gh api repos/<repo>/commits/<sha>` calls for commits whose subjects match the autofix prefixes — mirroring §20.1's spoof-defence identity check against `.author.login` / `.committer.login`. Worst-case API cost per gate invocation is O(number of advancing commits with autofix-prefix subjects), which is 0 in the common case.

- **Fail-open on `ADVANCE=unknown`**: any detection failure (fetch error, `gh api` error, empty advance set despite non-equal tips, unset `GITHUB_REPOSITORY`, missing GitHub-attributed identity) continues the cycle normally. Mirrors §20.1's fail-open doctrine — a GitHub API blip must not cause the review to be silently dropped.

- **`ADVANCE=self_only` also continues**: when every advancing commit is attributed to `AUTOFIX_BOT_LOGIN` (GitHub-attributed logins, not `.commit.author.email`), the gate logs `AUTOFIX_PRE_{EDITOR,PUSH}_SELF_ADVANCE ... action=continue` and falls through. In practice this is rare (the `pr-autofix-${PR}` concurrency group with `cancel-in-progress: false` prevents parallel autofix runs per PR), but if it does happen the existing `merge-retry` block handles the fast-forward merge on clean hunks and the existing hard-fail surfaces real conflicts.

- **Merge-conflict hard-fail at push-retry remains**: the existing `Aborting merge and failing the job; refusing to silently resurrect conflict markers` path at the end of the merge-retry loop stays. It is a narrow residual window (external push lands between the pre-push gate firing and the actual `git push`) and the loud failure is preferable to silently merging on the wrong side.

Log prefix contract (stable — renames are breaking changes per CLAUDE.md §6):

- `AUTOFIX_PRE_EDITOR_STALE_BASE pr=<n> local_sha=<sha> target_branch=<ref> action=soft_exit` — pre-editor gate detected external advance; editor + downstream steps skipped.
- `AUTOFIX_PRE_EDITOR_SELF_ADVANCE pr=<n> local_sha=<sha> target_branch=<ref> action=continue` — pre-editor gate saw `self_only` advance; continuing.
- `AUTOFIX_PRE_EDITOR_BASE_FRESH pr=<n> local_sha=<sha> target_branch=<ref>` — pre-editor gate saw no advance.
- `AUTOFIX_PRE_EDITOR_UNKNOWN pr=<n> local_sha=<sha> target_branch=<ref> action=fail_open` — pre-editor detection failed; fail-open.
- `AUTOFIX_PRE_PUSH_STALE_BASE pr=<n> local_sha=<sha> target_branch=<ref> action=soft_exit` — pre-push gate detected external advance; push + retrigger skipped.
- `AUTOFIX_PRE_PUSH_SELF_ADVANCE` / `AUTOFIX_PRE_PUSH_BASE_FRESH` / `AUTOFIX_PRE_PUSH_UNKNOWN` — analogous to the pre-editor variants.

Operational rules:

- Renames of `AUTOFIX_STALE_BASE_SKIP` (env var), `INITIAL_HEAD_SHA` (env var), `check_external_branch_advance.sh` (helper name), or any of the log prefixes above are **breaking changes** per CLAUDE.md §6. The env var is referenced by every downstream gated step's `if:` condition — renaming it without keeping the old one active will silently un-gate those steps.
- The pre-editor gate must stay **after** the reviewer consensus memory record step (reviewer artifacts are discarded by intent — Q4/A) and **before** any step that invokes `review_apply_fixes.sh` or installs project dependencies. Moving it earlier gives a tighter miss window at the cost of slightly less data; moving it later partially defeats the purpose (the editor is the expensive step).
- The pre-push gate must stay as the **first** lines of the "Push all pending commits" `run:` block — before `git remote set-url` and before the push-retry loop. Do not move it into a separate step: a new step would re-evaluate `if: env.DID_COMMIT == 'true' || env.CONFLICT_RESOLVED == 'true'` separately from the push and create a race where the push's `if:` fires but the gate doesn't.
- Consumer repos must include `check_external_branch_advance.sh` in their bootstrap copy. It is part of `REQUIRED_BOOTSTRAP_SCRIPTS` in `review_autofix.yml`'s "Stage workflow support files" step — do not move to the optional list without also adding graceful-degrade behaviour in both gates.

API cost audit (CLAUDE.md §15):

- **Pre-editor gate**: 1 `git fetch` (local; no API), then 0 `gh api` calls on the hit path (external advance with non-autofix subject — the common human-push case). Up to N `gh api commits/<sha>` calls on the rare path where advancing commits have autofix-like subjects and identity must be verified, where N = count of such commits (usually ≤ 2). Net API cost is strongly negative on the hit path — 1 editor invocation (codex-cli, ~10-20 min wall time, bulk of token spend) is saved per skip.
- **Pre-push gate**: same shape as pre-editor. On hit: saves the merge-retry `git fetch` + `git merge` attempt, avoids the `##[error]` + `phase_failed` ledger write, and avoids the follow-up `workflow_dispatch` retrigger call.
- Neither gate caches across invocations — each is scoped to a single run and per-PR. Cycle-local caching (§17) is not applicable because the two gates run at different wall-clock times (~10-20 min apart) and the remote tip can advance between them.

Safety net:

- If both gates mis-fire and incorrectly soft-exit on an advance that was in fact our own autofix (e.g. identity API returned stale data), the orchestrator stall cron (`internal-orchestrate-poll.yml`, cron `*/30 * * * *`) detects the stalled `ai:done` phase and re-dispatches `review_autofix.yml` via `workflow_dispatch`. Worst-case recovery window: ~30 min. Same safety net as §20.1.
- If both gates fail-open on detection error and the push subsequently hard-fails at the merge-retry step, the existing `phase_failed` ledger entry + Telegram failure alert fires as before. Detection failure gracefully degrades to the pre-gate behaviour.

### 20.3 Ledger Persistence (cache-backed) and the retained `LEDGER_ONLY_COMMIT` flag

§20.1's skip used to create an interaction bug: when a review pass produced no productive edit but `scripts/review_issue_ledger.sh` still wrote `REVIEW_LEDGER_PATH`, the `commit_changes` step produced an `[ai-autofix]` commit whose only tracked path was the ledger. That commit set `DID_COMMIT=true`, which historically gated three "clean review" steps (ready-to-merge label, enable auto-merge, telegram success) out of firing. The subsequent `pull_request.synchronize` event was then skipped by §20.1, so there was no follow-up run in which those gates could fire. Result: a PR that the editor cleared as no-change got stuck in `mergeable_state=clean` indefinitely (see PR #1472).

In the default configuration the ledger path is now `.ai/review_issue_ledger/pr-${PR_NUMBER}.txt` (per-PR, to prevent cross-PR merge conflicts on main) and is **gitignored**; cross-iteration persistence is handled by `actions/cache` restore/save steps wrapped around `Apply fixes with editor model` in `review_autofix.yml` (keys of the form `review-ledger-<repo>-pr-<N>-<run_id>-<run_attempt>` with `restore-keys: review-ledger-<repo>-pr-<N>-`). The ledger is updated on disk each iteration but never staged, so the ledger-only commit scenario the rest of this section describes **cannot manifest in the default configuration**. The contract below remains enforced because consumer repos that override `REVIEW_LEDGER_PATH` to a tracked path (or force-add the default path) still produce ledger-only commits, and the auto-merge gates must remain correct in that mode.

Current model (post-#1469 fix):

- `jobs.codex-agent.steps.commit_changes` writes a second signal alongside `DID_COMMIT` / `did_commit`: **`LEDGER_ONLY_COMMIT`** (env) and **`ledger_only_commit`** (step output). The flag is `true` iff `git diff-tree --no-commit-id --name-only -r HEAD` emits exactly one path equal to `${REVIEW_LEDGER_PATH:-.ai/review_issue_ledger/pr-${PR_NUMBER:-0}.txt}`. When `commit_changes` runs, its default is `false` (including the no-commit branch). In the default configuration the ledger path is gitignored and never staged, so this comparison never matches and the flag is always `false`; the detector exists for consumer repos that override `REVIEW_LEDGER_PATH` to a tracked path. If `commit_changes` is skipped (e.g. `max_iterations_reached` / `PR_CLOSED` short-circuits), the env var and step output are undefined; downstream `if` expressions must not rely on the flag being set in that case (the `did_commit != 'true'` clause they OR with already handles the skipped-commit branch).
- **Five gates** OR `env.LEDGER_ONLY_COMMIT == 'true'` into their existing `steps.commit_changes.outputs.did_commit != 'true'` clause:
  1. `Detect editor-claimed-but-uncommitted changes` (sets `EDITOR_CHANGES_LOST`) — must still run to verify the editor's "no edit" claim.
  2. `Validate editor no-op disposition` (sets `EDITOR_NOOP_SUSPICIOUS`) — same reason.
  3. `Mark linked issues ready to merge` — applies `ai:ready-to-merge`.
  4. `Enable auto-merge on PR` — `gh pr merge --squash --auto`.
  5. `Telegram success` — DEBUG-level completion notify.
- The push step (`DID_COMMIT == 'true' || CONFLICT_RESOLVED == 'true'`) is **not** touched. Ledger-only commits still push so `scripts/review_issue_ledger.sh` can read its own prior state on the next iteration — losing the ledger push would reset `persist_count` and re-open already-resolved issues as fresh findings.
- The §20.1 gate skip is **not** touched either. The subsequent `synchronize` run is still skipped; clean-review work happens in the same run that produced the ledger-only commit.

Gates that still OR `LEDGER_ONLY_COMMIT == 'true'` with `did_commit != 'true'` (legacy, no-op in default config):

1. `Detect editor-claimed-but-uncommitted changes` (sets `EDITOR_CHANGES_LOST`).
2. `Validate editor no-op disposition` (sets `EDITOR_NOOP_SUSPICIOUS`).
3. `Mark linked issues ready to merge`.
4. `Enable auto-merge on PR`.
5. `Telegram success`.

Cache persistence contract:

- `jobs.codex-agent.steps.Restore review-issue ledger` runs before the editor; `jobs.codex-agent.steps.Save review-issue ledger` runs immediately after `Apply fixes with editor model` (before `commit_changes`) with `if: always()` so the ledger for the current iteration is persisted even if a later step (push, conflict resolver) fails.
- The save step writes `.ai/review_issue_ledger/` + `${REVIEW_LEDGER_PATH}` through `actions/cache/save@v4`, gated by `if: always() && steps.retrigger_guard.outputs.max_iterations_reached != 'true' && env.PR_CLOSED != 'true'`, with `continue-on-error: true` to fail open on cache upload errors.
- The key includes `${{ github.repository }}` plus `run_id` + `run_attempt` so each run produces a distinct, repository-scoped cache entry; the `restore-keys` fallback gives every new iteration the latest previously-saved state for the same PR. GitHub's LRU eviction is 7 days since last access, which is well inside any active autofix cycle.
- Missing cache on first run is handled by `scripts/review_issue_ledger.sh` by treating every current issue as `NEW`; `ledger_reset=1` is emitted only for malformed prior-ledger content.

Operational rules:

- Renames of `LEDGER_ONLY_COMMIT`, `ledger_only_commit`, or the env var's default path reference `${REVIEW_LEDGER_PATH:-.ai/review_issue_ledger/pr-${PR_NUMBER:-0}.txt}` are breaking changes per CLAUDE.md §6. Add alongside.
- The ledger-path comparison must use `${REVIEW_LEDGER_PATH:-.ai/review_issue_ledger/pr-${PR_NUMBER:-0}.txt}` (not a hardcoded constant) so a consumer repo overriding `REVIEW_LEDGER_PATH` does not silently lose the auto-merge path — the detector and `scripts/review_issue_ledger.sh` must agree on the path. Both sides of the comparison are passed through a local `normalize_rel_path` shim (`sed -e 's#^\(\./\)\+##' -e 's#//*#/#g' -e 's#/$##'`) so equivalent relative spellings (`./.ai/...`, `.ai//...`, trailing slash) match the canonical form that `git diff-tree --name-only` emits. Absolute `REVIEW_LEDGER_PATH` values still pass through the same shim (the leading `/` is preserved because the first sed expression only strips `./`), so an absolute path's normalized form remains absolute and will not agree with git's relative-path output — the detector correctly declines to mark such commits as ledger-only.
- Do **not** extend `LEDGER_ONLY_COMMIT=true` to multi-file commits that happen to include the ledger. The signal's entire meaning is "the only tracked change is bookkeeping"; a commit that also touches runtime paths represents a productive edit and the existing `DID_COMMIT=true` path correctly blocks the clean-review gates until the next verification pass.
- If a future change introduces another always-written bookkeeping path, add it to the detector's single-path comparison as an equal-sized union (e.g. "commit contains exactly the ledger **and** the new bookkeeping file, nothing else"); do not loosen to a subset check.

### 20.4 Autofix Continuation

§20.1's skip is measured for the **verification** case (an `[ai-autofix]` commit whose reviewer panel would re-surface the findings already fixed in the preceding run). It is **not** correct for the **continuation** case — when the editor made a productive code edit the downstream state has genuinely changed, and a follow-up reviewer+editor pass is needed to either surface newly-introduced findings or terminate the cycle via the clean-review tail (§20.3). Pre-continuation the only path to that follow-up run was the orchestrator stall cron (`internal-orchestrate-poll.yml`, `*/30 * * * *`), which detects stalls via linked-issue phase timers. That path is unavailable for non-orchestrator PRs (branches like `claude/*` or any human-authored PR whose body does not reference an orchestrator-pipeline issue), so those PRs could remain idle indefinitely after a productive autofix commit.

Contract:

- `jobs.codex-agent.steps.Re-trigger review via workflow_dispatch` emits a `workflow_dispatch` for the same PR when the just-pushed commit was a **productive** autofix edit: `DID_COMMIT=true` AND `LEDGER_ONLY_COMMIT!=true` AND `CONFLICT_RESOLVED!=true`.
- Ledger-only commits are **not** continuation candidates — the clean-review tail in the current run already marks the PR ready-to-merge and enables auto-merge (§20.3). Continuing would wastefully re-run the reviewer panel on a tree the editor has already cleared.
- Conflict-resolved commits (`CONFLICT_RESOLVED=true`) continue to dispatch via the legacy path, unchanged from pre-continuation behaviour.
- `workflow_dispatch` bypasses the gate job's `self_triggered_autofix` skip (§20.1) by design; the continuation is a first-class successor run, not a spurious verification pass.
- Opt-out: set repository variable `AUTOFIX_CONTINUATION_ENABLED=false` (default `true`). This restores the pre-continuation behaviour where productive `[ai-autofix]` commits relied solely on `AUTOFIX_SKIP_SELF_TRIGGERED` and the stall cron.

Pre-dispatch guard (continuation path only):

- **Settle delay.** The step `sleep`s `AUTOFIX_CONTINUATION_SETTLE_SECS` seconds (default `10`, clamped `1..60`) after the push completes but before dispatch, to let GitHub's internal indices catch up — a newly-dispatched run that immediately checks out the new HEAD SHA can otherwise race the push replication. Tunable via repository variable.
- Iteration-cap handling remains in the dispatched run's in-workflow guard (`review_autofix.yml` step `Count autofix iterations`, id `retrigger_guard`), which gates reviewers/editor and then routes exhausted runs to the `rb_judge`/review-blocked path.
- The guard does not apply to the conflict-resolved dispatch path — that path keeps its pre-continuation timing and controls (the existing `AUTOFIX_RETRIGGER_PEER_WAIT_SECS` peer-wait still runs).

Alerts:

- The continuation path is **silent** — no Telegram notification is emitted. Other existing alerts in the same run (clean-review success, editor-changes-lost, review-blocked) are unaffected.
- Stall-cron `retrigger_review` alerts (`Stall recovery: re-triggered review for PR #<n>…` in `scripts/orchestrate_poll_process.sh:4351`, and the standalone-path analogue at `:5854`) are unchanged. They continue to fire only when the stall cron's phase-timer threshold actually trips for an orchestrator-tracked issue — which should now be rare for the post-autofix case because the continuation path resolves it in the same run.

Observability / log prefixes:

- `AUTOFIX_DISPATCH_SKIPPED reason=self_triggered_autofix pr=<n> current_run=<r> source=post_commit_retrigger continuation_enabled=<true|false> ledger_only=<true|false>` — the existing skip log, now with two additional key=value pairs so downstream analysis can distinguish: (a) continuation globally disabled, (b) ledger-only commit (clean-review handles auto-merge), (c) legacy pre-continuation opt-out via `AUTOFIX_SKIP_SELF_TRIGGERED=false`. The prefix is unchanged for backward compatibility with `scripts/analyze_workflow_logs.py`.
- `AUTOFIX_CONTINUATION_DISPATCH_ISSUED pr=<n> current_run=<r> settle_secs=<s> source=post_commit_retrigger` — continuation proceeded to the dispatch chain after settle delay (still subject to the existing peer-dedup at `AUTOFIX_DISPATCH_SKIPPED reason=peer_inflight` / `sync_event_inflight`).
- `AUTOFIX_DISPATCH_ISSUED reason=no_peer_detected pr=<n> current_run=<r> source=post_commit_retrigger continuation=true` — final dispatch confirmation for continuation-triggered runs; non-continuation paths keep the historical log without the `continuation=true` suffix.

API cost audit (CLAUDE.md §15):

- Settle is a `sleep`; no API calls.
- Dispatch uses the existing `gh workflow run` call chain (direct `review_autofix.yml` → caller-workflow fallback). No new `gh api` call is added by the continuation path.

Operational rules:

- Renames of `AUTOFIX_CONTINUATION_ENABLED`, `AUTOFIX_CONTINUATION_SETTLE_SECS`, or the log prefixes above are breaking changes per CLAUDE.md §6. Add alongside the old name and continue emitting the old log line in parallel for at least one stable-channel cycle before removing.
- Do not raise `AUTOFIX_CONTINUATION_SETTLE_SECS` clamp beyond `60` without re-evaluating the `timeout-minutes: 180` on `codex-agent` — a long settle on a failing dispatch loop could eat budget otherwise reserved for the editor.
- Do not move or weaken the target run's `retrigger_guard` cap gating (`max_iterations_reached`) without preserving the terminal `rb_judge` / review-blocked path; continuation relies on that in-run guard for cap exhaustion handling.
- Non-orchestrator PR coverage: continuation is the primary mechanism because the stall cron does not scan PRs that have no orchestrator-pipeline-labelled linked issue. When the cap is reached for such PRs the `rb_judge` step (gated by `ENABLE_REVIEW_BLOCKED_JUDGE`, default `true`, `review_autofix.yml` step `Mark linked issues review-blocked (autofix exhaustion)` at around line 2404) decides the terminal action. See README `ENABLE_REVIEW_BLOCKED_JUDGE` and the §20.4 Judge-at-cap note.

### 20.5 Failure-Comment Attribution (`EDITOR_SUMMARY_POSTED`)

`jobs.codex-agent` posts two distinct PR comments at end-of-run:

- The **editor summary** (step `Post editor summary comment`, around line 1734 of `review_autofix.yml`) is gated on `!cancelled() && ...`, so it runs even on `failure()`. This is intentional — when an editor summary is available but a downstream step failed, we still want that audit trail on the PR thread.
- The **failure notification** (step `Post review-blocked comment on PR (workflow failure)`, around line 3259) is gated on `failure() && env.PR_CLOSED != 'true'`.

When a step *after* the editor summary fails (push race against a concurrent push, conflict resolver abort, auto-merge config error, telemetry plumbing), both steps fire and the PR thread shows two comments 10–30s apart. The default failure body — "encountered an error and could not complete. This may be due to an editor failure, missing dependencies, or an infrastructure issue" — directly contradicts the success-looking editor summary above it and mis-attributes the failure for the human reader.

Contract:

- The editor-summary post step writes **`EDITOR_SUMMARY_POSTED=true`** (env, not a step output) only inside the success branch of the `gh api .../comments` call. On retry exhaustion (the `else` branch that emits `::warning::Unable to post editor summary comment after retries.`) the variable is left unset, since the summary comment is then not visible to the reader and the generic failure body is the right thing to post. This signal guarantees only summary-comment visibility, not editor-stage success.
- The failure-notification step branches its `BODY` on `${EDITOR_SUMMARY_POSTED:-false}`:
  - When `true` — post a narrower "AI review/autofix encountered a post-editor failure" body that names the downstream step domains (push, conflict resolver, label/auto-merge, telemetry) and notes that the summary comment is visible, not that editor execution necessarily succeeded.
  - When unset/false — post the original generic body (true editor / dependency / infra failure path).
- The label step (`Mark linked issues review-blocked (workflow failure)`) and `Telegram failure` are **not** gated on `EDITOR_SUMMARY_POSTED`. A failure is still a failure regardless of which comment variant is appropriate; the linked issue still needs `ai:review-blocked` and the operator still needs the Telegram alert.

Operational rules:

- Renames of `EDITOR_SUMMARY_POSTED` are breaking changes per CLAUDE.md §6. Add alongside.
- Do **not** set `EDITOR_SUMMARY_POSTED=true` from any step other than `Post editor summary comment`'s success branch. The signal's meaning is "the editor summary comment is on the PR thread right now"; setting it speculatively from any other step would re-introduce the contradictory-comment regression in the post-failure case.
- This change does **not** address the underlying push race that originally surfaced the regression (concurrent push to the PR branch while the workflow's editor was rewriting the same hunk → `git merge` conflict during push retry → step exit 1). That hardening is deliberately out of scope; the comment-attribution fix is the minimum surface area for the reader-facing bug.

API cost audit (CLAUDE.md §15):

- Zero new `gh` calls. The branch is a local `if [ "${EDITOR_SUMMARY_POSTED:-false}" = "true" ]; then ... fi` around the existing single `gh api .../comments` POST. No new batched data, no new cache.

---

## FINAL REMINDER

If uncertainty exists: **ASK (multiple-choice). DO NOT EXECUTE.**

Accuracy > speed. Safety > convenience. Backward compatibility is mandatory.
