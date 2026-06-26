# CLAUDE.md — Project Instructions (Interactive Claude Code Sessions)

This file is the system context for **interactive Claude Code sessions only** —
the human-driven coding work, where you can pause, ask clarifying questions,
and confirm decisions before acting.

The unattended pipelines (codex-cli driven: clarify, plan, orchestrate, judge,
implement, review autofix, conflict resolver, validate, workflow log analysis)
read `unattended_system_instructions.md` instead and **never see this file**.
That file deliberately omits the STOP-and-ASK rules below — those rollouts
must be biased to action.

These instructions are mandatory and must be followed before any action.

---

## PRE-TASK MANDATORY CONTEXT LOADING

Before any task, read:
- `README.md`
- `agents.md`
- all `/db/contracts/*.yml` (or `.json`) relevant to collections that may be touched

If any are missing or unclear: **STOP and ask using the mandatory Q/A format.**
Never assume undocumented behavior.

---

## §0. Prime Directive (NON-NEGOTIABLE)

If you are **not 100% certain** the outcome matches the user's expectations:
**STOP. ASK. DO NOT PROCEED.** — even if the task looks trivial or the intent
seems obvious.

---

## §1. Core Priorities (Strict Order)

1. Security
2. Correctness & safety
3. Backward compatibility
4. Operational clarity
5. Performance
6. Speed

---

## §2. Always-On Ask-First Mode

Ambiguity is a **hard stop**.

Before asking questions:
- Restate your understanding of the task.
- Study the repo to avoid avoidable questions.
- Identify all blocking uncertainties.

Ask clarifying questions **before** modifying code, schemas, configs, scripts,
docs, migrations, or infrastructure.

### Clarification Batching
Ask **all known questions in a single batch**. Follow-ups only if answers
introduce new ambiguity.

### Mandatory Question Format

Use stable identifiers `Q1`, `Q2`, etc. with letter-only answers
(`A`, `B`, `C`, or `A+C`).

Format:

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

Ask if any of these are unclear:
- **Scope:** which repo/module/service, runtime vs batch, prod/staging/dev.
- **Behavior:** expected behavior, edge cases, failure handling, safety constraints.
- **Interfaces:** API/CLI/env vars, backward compatibility, logging/observability.
- **Data/MongoDB:** collections, uniqueness rules, index contracts.
- **Operations:** timing, concurrency, rollback/recovery.

### Forbidden
- Guessing intent or applying "reasonable defaults" without confirmation.
- Silent refactors, cleanups, or speculative fixes.

---

## §3. Production Code Assumptions

All code is production-bound. Verify: logic correctness, error paths, race
conditions, idempotency, deployment safety.

---

## §4. Environment Variables

- Always provide defaults for new env vars unless explicitly told otherwise.
- Preserve all existing env var names.

---

## §5. Minimal Change Set

- Do NOT change formats, types, or unrelated logic.
- Do NOT reformat files unless required for the fix.
- Extend existing mechanisms — never compete with them.

---

## §6. Backward Compatibility / Naming Immutability

NEVER rename, remove, or repurpose existing identifiers (variables, functions,
classes, modules, CLI flags, env vars, URL paths, JSON/DB fields,
index/event/metric names, log keys) without asking first and detailing current
usage.

All renames are **breaking changes**. If a new name is needed:
- Add alongside the old one, accept both inputs, preserve old outputs,
  document aliases.

### New Identifier Uniqueness (STRICT)

When creating **new** variables — or any new identifier you introduce for a
task (functions, classes, constants, parameters, env vars, JSON/DB fields,
log/metric keys) — you MUST ensure the chosen name is unique and does not
clash with any existing identifier reachable in that scope. This is a
**strict, non-negotiable condition**.

- Before introducing a new name, check the surrounding function, file,
  module, and any imported / shared / global scope for an existing
  identifier of the same name. Never shadow or collide with existing
  variables, parameters, imports, globals, inherited members, or any other
  in-scope identifier.
- If the intended name is already taken, choose a distinct, descriptive
  alternative — do not reuse, shadow, or overload the existing one.
- This is the complement of the immutability rule above: §6 forbids renaming
  existing identifiers; this rule forbids new identifiers from colliding with
  them.

Section numbers in this file are also covered by §6 — they are referenced from
`.github/workflows/`, `scripts/`, and `prompts/` and must not be renumbered.

---

## §7. Output Requirements

In every final response:
- List all files changed with line ranges of major logic changes (skip
  formatting-only).
- If behavior changes: update `README.md` / `agents.md` with env vars,
  DB behavior, indexes, operational steps, failure modes.
- In user-facing replies, describe file changes as `edited path/to/file`
  rather than naming internal edit tools such as `Edit`, `Write`, or
  `apply_patch`; internal logs and code comments may still name concrete
  tools when useful.

---

## §8. Debugging & Diagnostics

If a problem's cause is unclear: add **diagnostic logging first**, not
speculative fixes. Logging must be structured, searchable, with context keys.

---

## §9. Code Style

- **Tabs** for indentation — except in formats where the language forbids
  tabs or mandates a different indentation token:
  - **YAML** (`.yml`, `.yaml`) MUST use **2-space** indentation. YAML spec
    disallows tab characters as indentation; `docker compose config` and
    every YAML parser will reject tab-indented YAML.
  - Makefile recipe bodies must use a literal TAB.
  - If a sub-directory pins a different convention via `.editorconfig`,
    honour that file for files it covers.
- Opening braces on a **new line**.

---

## §10. MongoDB Rules

### A) DB Contract
One contract per collection at `/db/contracts/<collection>.yml`. Must include:
collection name, indexes (keys, uniqueness, partials, collation), purpose,
business invariants, write entrypoints. Any query/write change must update
the contract.

### B) Index Registry
Single shared index module (e.g. `ensureIndexes`). No ad-hoc `createIndex` calls.

### C) Runtime Index Creation
Use distributed lock via `_locks` collection with lease expiry. Compare indexes
by name+keys+options. Never silently drop/recreate in prod.

### D) Unique Index Safety
Explicit null/missing/empty rules. Prefer partial unique indexes. Preflight
duplicate detection. Treat E11000 as expected in races.

### E) Idempotency
Require idempotency keys backed by unique indexes. Prefer atomic upserts.

### F) Transactions
Use sparingly, retry transient errors, keep scope minimal.

### G) Query/Index Alignment
Every query must have a matching index or documented justification.

### H) Operational Safety
Document index timing, expected output, failure modes, rollout considerations.

---

## §11. Task Checklist Completion Gate

When a user provides a task list for execution, convert it to a checklist.

Rules:
- Track and update checklist visibly in conversation.
- Mark items complete only after work is done or user confirms.
- Map every task; never skip or silently drop items.
- Complete all non-PR items before creating a PR (unless user approves splitting).
- If blocked: report failure, keep item open, await direction.

Scope: in PR review mode, applies only to new task lists in the current request.

---

## §12. PR Review Mode

**This §12 fully supersedes the prior "Intent Preservation / Forbidden /
Acceptance Criteria" version of §12 in this CLAUDE.md.** The parallel §12
in `codex.md` (and any rules in `unattended_system_instructions.md`) is
unaffected — unattended pipelines retain their own policies. Earlier
guidance to "not introduce new scope, abstractions, or behaviors" no
longer governs PR review work in interactive sessions; the proactive
policy below applies instead.

**Precedence in PR Review Mode.** While operating under §12, this section
takes precedence over §0 (Prime Directive), §2 (Always-On Ask-First Mode —
including its "Forbidden: Silent refactors, cleanups, or speculative
fixes" clause), and §5 (Minimal Change Set), for the proactive-fix
decisions enumerated in §12.B. §0 and §2 still govern items routed to
§12.D (the explicit ask-list) and any decision outside the PR-review
scope. §6 (naming immutability) and §10 (MongoDB contracts) remain hard
rules even under proactive scope and are NOT superseded.

When the user asks Claude to address PR review feedback — via `@codex change`
in a PR, a direct chat request, a `subscribe_pr_activity` event, or any
equivalent trigger — apply fixes with a **wide proactive scope**. Default to
action, not to asking. Only stop and ask on the genuinely ambiguous items
enumerated in §12.D.

### A) Single-PR Rule (NON-NEGOTIABLE)

All fixes — whether tied to the original PR scope or discovered out-of-scope
during review — MUST be committed to the same PR. Never spin off a follow-up
PR for "later cleanup". If the proactive scope is genuinely too large to fit
in this PR (review-blocker territory), STOP and ask whether to include all
of it or drop it — never split into a new PR.

### B) Auto-Apply Without Asking

Apply fixes proactively, without asking, when the issue falls into any of
these categories AND the fix passes the evaluation signals in §12.C
(high-confidence, low-blast-radius, no §6/§10 conflict):

- **Security:** injection (SQL/command/template), XSS, auth bypass, secret
  leaks, unsafe deserialization, missing authz checks, CSRF gaps.
- **Crash / data-loss:** null derefs, unhandled exceptions/rejections, race
  conditions, lost writes, leaking handles or connections, missing locks,
  off-by-one on persisted data.
- **Correctness defects:** wrong operator, swapped arguments, inverted
  condition, wrong env var, wrong field/index/collection name, incorrect
  return path.
- **Reviewer-flagged defects** with a clear, verifiable diagnosis that
  matches the code on re-read.
- **Missing error handling at system boundaries** — unvalidated user
  input, unchecked external API responses, IPC payloads, or unhandled
  failure modes that would surface in production.
- **Type / contract violations.**
- **Stale comments, misleading docs, wrong examples** that would mislead
  future readers.
- **Latent bugs in adjacent code** exercised by the same flow being fixed —
  proactive scope is explicitly in-bounds; fix them.
- **Production-breaking issues** identified anywhere in the touched files,
  whether or not the reviewer called them out.

### C) Weigh Before Acting

Even when a fix falls in §12.B, evaluate:

- **Reversibility** — prefer the cheaper fix (guard, null check, bounds
  check) over a structural change when it solves the defect.
- **Blast radius** — single function (act) vs many files (consider §12.D).
- **Confidence in the reviewer's diagnosis** — re-read the code; if the
  reviewer is wrong, surface that as a reply instead of applying.
- **Test coverage** — add or extend tests when fixing a defect that lacked
  coverage. Do not ship a behavior fix without verification.
- **Conflict with §6** — renames and removals of identifiers stay forbidden
  even under proactive scope. Add aliases alongside if needed.
- **Conflict with §10** — DB / index / contract changes still require the
  matching `/db/contracts/*` update; do not skip that.
- **Public contract or hot-path performance impact** — if either, treat as
  §12.D ask-territory.

### D) STOP and ASK (Q/A format) When

Even with the proactive default, ask before acting on:

- Renames or removals of any identifier covered by §6 (variables,
  functions, classes, modules, CLI flags, env vars, URL paths, JSON/DB
  fields, index/event/metric names, log keys — public or internal).
- Architectural refactors, new abstractions, module reorganization.
- Multiple plausible fixes with material tradeoffs (perf vs correctness,
  throw vs swallow, retry vs fail-fast, sync vs async).
- Behavior changes affecting documented contracts (`README.md`,
  `agents.md`, `/db/contracts/*`).
- DB schema or index changes that lack an obvious contract update path.
- Scope explosion — one review comment implies touching 10+ files. Ask
  whether to include all of it in this PR or drop it (per §12.A, no
  follow-up PR is allowed).
- Anything where Claude would be guessing at intent rather than fixing a
  verifiable defect.

### E) Commit and PR Hygiene

Since all fixes land in one PR (§12.A), commit hygiene is the only
separation tool — use it deliberately:

- **One commit per scope.** Commit the in-scope review feedback fixes
  separately from out-of-scope proactive fixes. Group related proactive
  fixes by theme (e.g. "fix null-safety gaps in user import path") rather
  than one commit per file.
- **Commit messages must link to the trigger** — for in-scope fixes, cite
  the review comment; for out-of-scope fixes, explain the proactive
  rationale (e.g. "Fix race in cache invalidation — discovered while
  addressing review comment on `cache.go:42`").
- **PR description must enumerate every out-of-scope fix** included in
  this round, so reviewers can locate them without diffing
  commit-by-commit. Use a "Proactive fixes included" subsection.

### F) Acceptance Criteria

After changes:
- Every reviewer-raised defect is fixed, surfaced as a disagreement, or
  asked about (§12.D).
- Every proactive fix is traceable to a category in §12.B and documented in
  the PR description (§12.E).
- §6 (naming) and §10 (MongoDB contracts) are still honored.
- All fixes are in this PR — none deferred to a follow-up.
- If no changes are needed at all: reply "No changes are needed."

### G) Autofix CI / Address-Comments Mode Add-ons

When Claude is invoked under the **autofix CI / address-comments mode** —
i.e. an **interactive Claude Code session** driven by a
`subscribe_pr_activity` event tied to a failing required check, an
`@codex change` / "address the review comments" request on a PR, or any
equivalent trigger that tasks the interactive session with making the
branch green and the review thread satisfied — the following are
first-class auto-apply categories on top of §12.B. Fix them without
asking.

This subsection governs **interactive sessions only**, consistent with
the preface at the top of this file (lines 7–10): the unattended
`review_autofix` pipeline reads `unattended_system_instructions.md` and
keeps its own policy, so the rules below do not flow into that pipeline
and must not be cited as if they did.

- **Lint / formatter / static-analysis failures**, **including failures
  whose offending line is outside the current PR's diff.** Owning a green
  branch is part of this mode, so a lint, formatter, or static-analysis
  violation surfaced by CI must be fixed even when the violation was
  introduced by an earlier commit on this branch, lives in a file the
  current PR did not otherwise touch, or is in code Claude has not
  modified in this session. The "scope explosion" STOP condition in
  §12.D does NOT apply to mechanical lint sweeps — bring the branch
  green even if that touches many files. §6 (naming immutability)
  still binds: if the only mechanical fix would rename a public
  identifier flagged by a style rule, route to §12.D instead of
  renaming.
- **Merge conflicts with the base branch.** Resolve them automatically
  so the PR is mergeable. Prefer the resolution that preserves both
  sides' intent over the resolution that drops one side; never silently
  discard either side's changes. When both sides genuinely conflict and
  the correct resolution is non-obvious from the diff (semantic intent
  unclear, both branches changed the same invariant in incompatible
  ways, or the resolution would alter a documented contract per §12.D),
  STOP and ask in Q/A format before committing the resolution. Record
  the resolution in the merge commit message and call it out in the PR
  description's "Proactive fixes included" subsection (§12.E).

These add-ons inherit the rest of §12 unchanged: §12.A (one PR — lint
sweeps and conflict fixes land in this PR, never a follow-up), §12.C
(weigh reversibility, blast radius, and §6/§10 conflicts before acting),
§12.E (commit hygiene — group the lint sweep into its own commit
distinct from the in-scope review fixes; record the conflict resolution
in its own commit), and §12.F (acceptance criteria).

---

## §13. Repository Hygiene

- Never write into `.git/**` (no artifacts, caches, or bytecode).
- Set `PYTHONDONTWRITEBYTECODE=1` for Python tooling on repo files.
- Treat `__pycache__`/`*.pyc` under `.git/` as invalid state.

---

## §14. Consumer Repo Registry

When a new consumer repository is onboarded (i.e. it copies workflow templates
from `workflow-templates/`), **always** add it to
`.github/ai/consumer_repos.json`. This JSON array lists all repos that receive
`repository_dispatch` events when a new `@stable` release is tagged, enabling
immediate workflow wrapper updates.

- File: `.github/ai/consumer_repos.json`
- Format: JSON array of `"owner/repo"` strings
- The `GH_PAT` used in release workflows must have `repo` scope on every
  listed consumer repo for the dispatch to succeed.

---

## §15. GitHub API Call Hygiene (MANDATORY)

GitHub REST and GraphQL rate limits are a shared resource across every
orchestrator and issue-processing job. Before writing **any** new `gh api`,
`gh_retry`, `_safe_gh_jq`, `gh issue ... --json`, `gh pr ... --json`,
`gh run list`, or direct `curl https://api.github.com/...` call, you MUST
check whether the data can be obtained from an existing call in the same
code path and merged or batched with it.

Rules:

- **Check first, add second.** Search the surrounding function and file for
  existing `gh` invocations hitting the same issue/PR/repo scope. If one
  exists, extend it (add a JSON field, add a GraphQL alias, reuse its cached
  result) instead of creating a new call.
- **Prefer batched GraphQL over per-item REST.** When fetching data for N
  items (issues, PRs, comments, labels, timeline events), use an aliased
  GraphQL query (see `_fetch_candidate_issue_details_graphql` and
  `_fetch_linked_pr_status_graphql` in `scripts/orchestrate_poll_process.sh`
  for the pattern) so N items cost `ceil(N / batch_size)` API calls, not N.
- **Cycle-local caches are first-class.** Orchestrator loops that need the
  same data for many iterations MUST prefetch once into a shell/file cache
  (`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`,
  `_candidate_details_json`) and have the inner loop read from the cache.
  Adding a per-iteration `gh api` call inside such a loop is a review-blocker.
- **Fail open on cache miss.** A cache/prefetch failure must never block the
  caller — fall back to the smallest safe legacy call, not a tight retry loop.
- **Document the batching contract.** When you add a batched helper, spell
  out in the function docstring the input shape, output shape, number of API
  calls issued, and fail-open behaviour so future callers can reuse it
  without re-reading the implementation.

If you need a new data shape that truly cannot be satisfied by any existing
call, add a comment above the new invocation explaining which existing calls
you audited and why they were insufficient.

---

## §16. Task Delegation

When spawning subagents, use the cheapest model that can handle the task:
- Haiku: bulk mechanical tasks - no judgment needed
- Sonnet: scoped research, code exploration, synthesis
- Opus: only for real planning or tradeoff decisions

Spawn rules:
- Haiku cannot spawn subagents. If it needs to, return to parent.
- Max spawn depth: 2
- Subagents escalate to parent, never self-escalate model tier

When to use a subagent:
- Treat each subagent like a productive junior engineer who cannot ask
  follow-up questions once started; tasks that may need mid-rollout
  clarification should stay with the parent.
- Do use subagents for well-specified multi-file scaffolding that should
  not need follow-up clarification.
- Do use subagents for mass renames, bulk generation, or repetitive lint
  sweeps.
- Do use subagents for parallel independent research reads, web fetches,
  or grep sweeps.
- Do not use subagents for exploratory codebase mapping where the right
  questions emerge mid-task.
- Do not use subagents for architecture or tradeoff decisions that should
  stay with the parent.
- Do not use subagents for debugging that may need follow-up user
  questions.

---

## §17. Preferred Tools

- Public pages → WebFetch (free, text-only)
- Dynamic pages / auth walls → agent-browser CLI
- PDFs → pdftotext (not Read tool)
- Repeated fetch patterns → wrap as reusable tool

---

## §18. Automation Bias (Reduce Human Involvement)

The overarching goal of every task, plan, and change is to **minimize
human-in-the-loop**. Operations should run from code on a schedule, not
from an operator typing into a shell. Plans that require an operator to
run a script, run a mongo command, or babysit a process are incomplete
and must be revised before they reach the orchestrator.

### A) No Standalone Manual Scripts (Hard Rule)

When making changes — or drafting plans for the orchestrator to
implement — do NOT introduce standalone scripts that require manual
shell invocation to run. Fold the work into an existing script or
workflow that already runs automatically.

If a manual-invocation script appears to be the only viable option,
**STOP and ask** in the Q/A format (§2) before adding one, and record
the justification in the plan. Default answer is "no — wire it in
instead."

### B) Wire Into the Scheduler

Any new operation that is not folded into an existing script MUST be
wired into the existing scheduler / PR-push automation so it runs on
PR push (or the relevant trigger) without operator action. Plans MUST
cite the specific workflow / cron file the change will touch so the
orchestrator knows exactly where to wire it. Code changes MUST land the
wiring in the same PR — never plan or accept a "wiring lands later"
handoff.

### C) Long-Running Supervisor

If the work needs to run continuously, react to events between PRs, or
supervise other automation, a long-running supervisor is required. If
no suitable supervisor already exists, **one must be created as part of
the same change** — do not defer it.

Plans MUST specify:
- Whether a new supervisor is being introduced or an existing one
  extended.
- Lifecycle: entry point, restart policy, shutdown signal handling,
  crash-recovery behavior.
- How the supervisor is wired into the scheduler / startup automation
  so it comes up without operator action.

A supervisor that needs an operator to start it is not a supervisor —
it is a manual script (§18.A) and is subject to the same hard rule.

### D) Database Operations Run From Code (Hard Rule)

Database operations — one-time backfills, schema migrations, index
rebuilds, long-running maintenance, recurring cleanup — MUST run from
code with appropriate gates so they execute only as much as needed.
Do NOT plan or accept "operator runs this mongo shell command" steps.

Gates must use the patterns already established in §10:
- Idempotency keys backed by unique indexes (§10.E) and atomic upserts
  so repeated runs converge instead of duplicating.
- Distributed locks via `_locks` with lease expiry (§10.C) for any
  operation that must run at most once across processes.
- Explicit "already applied" sentinels (run flags, marker documents,
  versioned migration records) so the gate is observable and
  auditable.

If the right gate is not obvious for a given DB operation, route to
§2 (STOP and ASK) — do not ship an ungated DB operation.

### E) Plan Output Requirements

Every plan for the orchestrator MUST surface, in a dedicated section
near the top of the plan:

- Whether the change introduces a new script, extends an existing one,
  or only modifies existing code.
- The exact scheduler / PR-push entry point the change wires into
  (file path + trigger).
- Whether a new long-running supervisor is required (§18.C), and if so
  its lifecycle and wiring.
- For DB work: which gate pattern (§18.D) applies and where the gate
  lives in code.
- For any new single-use / long-running script or supervisor: the
  registry entry to be added to `docs/scripts-pending-removal.md`
  (§18.F) — removal trigger and removal preflight checks.

Plans that omit any of these MUST be revised before the orchestrator
implements them.

### F) Future-Removal Registry

Every single-use script, long-running script, and long-running
supervisor introduced under §18.A–C MUST get an entry in
`docs/scripts-pending-removal.md` **in the same PR** that introduces
it. The registry is one centralized doc — do not create per-script
removal docs.

Each entry MUST include:
- **Script path** — the script, supervisor entry point, or workflow
  file the entry is about.
- **Introduced in** — PR number and date the script landed.
- **Type** — `single-use`, `long-running`, or `supervisor`.
- **Removal trigger** — the concrete condition that makes removal
  safe (e.g. "after backfill `X` completes for all docs", "when
  feature flag `Y` is GA for 30 days", "when supervisor v2 replaces
  v1"). If no sunset applies, use **"permanent — review annually"** —
  do not omit the field.
- **Removal preflight checks** — explicit list of checks that MUST
  pass before the script is removed, to verify the script has
  already done its job. Each check names the exact command, query,
  or signal to inspect and the expected result / threshold. These
  checks are what protects against removing a script that hasn't
  finished its work.
- **Owner** — GitHub handle of the person / agent who owns the
  removal decision.

When a script is removed from the codebase, **delete its entry from
the registry in the same PR**. The registry is a live list, not an
audit log — there is no "removed" archive section. Git history is the
audit trail.

If a script is renamed or extended, update its entry (path, trigger,
preflight checks) in the same PR — §6 (naming immutability) still
applies, so renames require the §2 ask flow first.

---

## §19. PR Body Auto-Close Keyword Discipline (MANDATORY)

GitHub auto-closes issues referenced with keywords like `Fixes #N`,
`Closes #N`, or `Resolves #N` in a PR body when the PR merges into the
default branch. For orchestrator tracking issues — any issue carrying
the `ai:orchestrator-tracking` label — this silently kills the
orchestrator's state machine: once the tracking issue closes, the
poller treats the project as done and stops dispatching the remaining
waves.

Rules:

- **NEVER** use auto-close keywords (`close`, `closes`, `closed`, `fix`,
  `fixes`, `fixed`, `resolve`, `resolves`, `resolved`, case-insensitive)
  followed by a reference to an `ai:orchestrator-tracking` issue in a
  PR body, PR title, commit message, or merge commit trailer. Use
  `Refs #N` or `Related to #N` for semantic linkage instead.
- A PR that fixes a **blocker bug surfaced by an orchestrator project**
  is not a fix for the project itself — `Refs #N` is still the correct
  linkage, not `Fixes #N`. The orchestrator project closes itself via
  its own state machine when all waves complete.
- Before drafting a PR body that references an orchestrator project's
  tracking issue, verify the target's labels (`gh issue view <N> --json
  labels` or `mcp__github__issue_read` with `method=get_labels`). If
  `ai:orchestrator-tracking` is present, the auto-close keywords are
  forbidden against that issue number.
- This rule is not superseded by §12 (PR Review Mode) or any proactive-
  fix scope. It applies to every AI-authored PR body in an interactive
  session.
- The rule is enforced by **two** mechanisms, both anchored on
  `scripts/lint_pr_body_auto_close.py`:
  1. The `Lint PR body for auto-close keywords against orchestrator-tracking
     issues` GitHub workflow (`.github/workflows/lint-pr-body-auto-close.yml`)
     runs on every pull request and fails the check when a violation
     is found.
  2. The unattended pipelines' PR-body composition paths invoke the
     same script as a pre-flight check before `gh pr create --body` —
     see `unattended_system_instructions.md` §21 and the
     `Pre-flight — lint PR body for auto-close keywords against
     tracking issues` step in `.github/workflows/implement.yml`.
  Both layers exist because §19 is a load-bearing invariant: a single
  violation kills the orchestrator's state machine for the project it
  targets.

Historical incident: PR #2760 used `Fixes #2734` in its body. `#2734`
was an `ai:orchestrator-tracking` issue for the integration-sync
resolver self-heal project. On merge, GitHub auto-closed `#2734` and
the orchestrator stopped dispatching waves 2-7; the bulk of the
project's planned phases never shipped (see
`docs/completed/integration-sync-resolver-self-heal-plan.md` and the
full forensic timeline in
`docs/postmortems/2026-05-18-project-2734-stall.md`).

## §20. CHANGELOG Style

All `CHANGELOG.md` entries follow `docs/changelog-style.md`. PR review checks new entries against that guide's structure, voice rules, and audience split.

---

## FINAL REMINDER

If uncertainty exists: **ASK (multiple-choice). DO NOT EXECUTE.**

Accuracy > speed. Safety > convenience. Backward compatibility is mandatory.
