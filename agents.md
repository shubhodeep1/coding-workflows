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

<!-- §OpenRouter Prompt Cache Instrumentation moved to ./probably_unnecessary_but_read_if_stuck.md — read it there if you need cache-instrumentation telemetry details. -->

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

<!-- §4 Environment Variables and §4a Post-Codex Recovery Docs Sync moved to ./probably_unnecessary_but_read_if_stuck.md — read it there if you need the full env-var reference (defaults, descriptions, consumer workflows) or the post-Codex docs-sync rules. -->

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

<!-- §13 Workflow Log Analysis Operations and §13a Workflow Log Analysis And Improvement Workflow moved to ./probably_unnecessary_but_read_if_stuck.md — read it there if you need workflow-log-analysis pipeline details. -->

## 14. Repository Hygiene

- Never write into `.git/**` (no artifacts, caches, or bytecode).
- Set `PYTHONDONTWRITEBYTECODE=1` for Python tooling on repo files.
- Treat `__pycache__`/`*.pyc` under `.git/` as invalid state.

---

<!-- §15 Semantic Cache Scope and §16 Validation Self-Healing moved to ./probably_unnecessary_but_read_if_stuck.md — read it there if you need semantic-cache scope rules or validation self-healing internals. -->

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

<!-- §18 Internal Wrapper Pin Policy, §19 Workflow Checkout Integration-Ref Contract, and §18 (duplicate-numbered) Orchestrator Integration-Sync Auto-Heal Hardening moved to ./probably_unnecessary_but_read_if_stuck.md — read it there if you need wrapper pin policy, integration-ref checkout contract, or orchestrator integration-sync auto-heal details. -->

<!-- §20 Autofix Retrigger Dedup (with subsections 20.1–20.10: Self-Triggered Autofix Skip, Mid-Run External-Push Gates, Ledger Persistence, Autofix Continuation, Failure-Comment Attribution, Empty-Editor-Output Retry Gate, Autonomous review-blocked Escape, Claude-Branch Skip, CI/Lint Check-Run Autofix Context, Editor-NoOp Suspicious Skip) moved to ./probably_unnecessary_but_read_if_stuck.md — read it there if you need autofix retrigger / dedup / oscillation / cap-handling internals. -->

## FINAL REMINDER

If uncertainty exists: **ASK (multiple-choice). DO NOT EXECUTE.**

Accuracy > speed. Safety > convenience. Backward compatibility is mandatory.
