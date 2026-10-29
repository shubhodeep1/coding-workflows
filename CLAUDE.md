# CLAUDE.md — Project Instructions (Interactive Claude Code Sessions)

This file is the system context for **interactive Claude Code sessions only** —
the human-driven coding work, where you can pause, ask clarifying questions,
and confirm decisions before acting.

The unattended pipelines (codex-cli driven: clarify, plan, orchestrate, judge,
implement, review autofix, conflict resolver, validate, workflow log analysis)
read `unattended_system_instructions.md` instead and **never see this file**.
That file deliberately omits the STOP-and-ASK rules below — those rollouts
must be biased to action.

---

## PRE-TASK MANDATORY CONTEXT LOADING

Before any task, read:
- the sections of `README.md` and `agents.md` (or `AGENTS.md` — whichever
  casing the repo root has; same file) that cover what the task touches.
  Both files can run to hundreds of kilobytes, so search them for the
  relevant sections instead of reading them end to end.
- all `/db/contracts/*.yml` (or `.json`) relevant to collections that may be touched

If any are missing or unclear: **STOP and ask using the mandatory Q/A format.**
Never assume undocumented behavior.

---

## §0. Prime Directive (NON-NEGOTIABLE)

If you are not 100% certain the outcome matches the user's expectations,
stop and ask before acting — even if the task looks trivial or the intent
seems obvious. In an interactive session a question costs one round trip;
a wrong guess costs a revert.

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

### Clear-Winner Assumptions

A `(RECOMMENDED)` mark alone is not a reason to ask: the format above puts one
on every question. When a decision has a clear winner, take it without asking
and list it as an assumption instead. A decision is a clear winner only when
**all** of these hold:

- One option is clearly correct: an existing repo convention, the code or
  docs require it, it follows from an answer the user already gave, or it is
  the only option that is not unsafe or breaking. If the rationale would
  start with "it depends", it is not a clear winner.
- It is cheap to undo: a change on the session's working branch, not an
  action outside the repository.
- It is not about what the user wants built (feature intent, scope, target
  repo or environment) and not a tradeoff the user should weigh.
- It is not ask-first anywhere else in this file, including: the PRE-TASK
  stop for missing or unclear context, §6 renames and removals, §10 DB /
  index / contract changes, §12.D, §18.A manual scripts, §18.D gates, §22.B,
  §23.C, §24.D and §29.B operations, §22.C / §24.F missing resource IDs, or
  any deletion of data.

Assumptions share the Q-ID sequence with the open questions and appear in the
same message, under an **Assumed** heading, before any work starts:

> **Assumed**
> - **Q3: \<question\>** — took **A** (\<option\>) because \<one-line reason\>.
>   Other options: **B** — \<description\>. Reply `Q3: B` to override.

If open questions remain, wait for the answers as usual; an override arrives
with them. If every decision was a clear winner, give the Assumed list at the
top of the reply and proceed. This subsection is the only exception §2 itself
makes to §0 and to the Forbidden list below (§12 keeps the precedence it
declares for PR Review Mode); everything else in §0 and §2 still applies.

### When to Ask

Ask if any of these are unclear:
- **Scope:** which repo/module/service, runtime vs batch, prod/staging/dev.
- **Behavior:** expected behavior, edge cases, failure handling, safety constraints.
- **Interfaces:** API/CLI/env vars, backward compatibility, logging/observability.
- **Data/MongoDB:** collections, uniqueness rules, index contracts.
- **Operations:** timing, concurrency, rollback/recovery.

### Forbidden
- Guessing intent or applying "reasonable defaults" without confirmation
  (a clear-winner assumption listed under **Assumed** is not a default).
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
  DB behavior, indexes, operational steps, failure modes. Additions to
  `agents.md` go in an `agents.d/` fragment (§30).
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

This §12 governs interactive sessions only. The parallel §12 in `codex.md`
(and any rules in `unattended_system_instructions.md`) is unaffected —
unattended pipelines retain their own policies.

**Precedence in PR Review Mode.** While operating under §12, this section
takes precedence over §0 (Prime Directive), §2 (Always-On Ask-First Mode —
including its "Forbidden: Silent refactors, cleanups, or speculative
fixes" clause), and §5 (Minimal Change Set), for the proactive-fix
decisions enumerated in §12.B. §0 and §2 still govern items routed to
§12.D (the explicit ask-list) and any decision outside the PR-review
scope. §6 (naming immutability) and §10 (MongoDB contracts) remain hard
rules even under proactive scope and are NOT superseded.

When the user asks Claude, in the session, to address PR review feedback,
apply fixes with a **wide proactive scope**. Default to action, not to
asking. Only stop and ask on the genuinely ambiguous items enumerated in
§12.D.

A `subscribe_pr_activity` event is **not** a trigger for this mode. PR
watching is disabled by §25, so no such event should ever reach an
interactive session; if one does, do not act on it — treat it as a
misconfiguration and report it. §12 is entered only by a direct request in
the session.

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

**INACTIVE — superseded by §25.** This subsection held add-ons for an
autofix CI / address-comments mode entered by a `subscribe_pr_activity`
event, which §25 disables. The heading stays so section references remain
stable (§6); the retired text is in git history and can be restored by
editing §25 in a reviewed change. When the user directly asks in the
session to fix CI or address review comments on a PR, plain §12 (A–F)
applies.

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

The incident behind this rule is recorded in
`docs/postmortems/2026-05-18-project-2734-stall.md`.

## §20. CHANGELOG Entries (MANDATORY)

**Never edit `CHANGELOG.md` directly.** Write a fragment file instead.
Newest-entry-first insertion means every concurrently-open PR edits the same
hunk at line 1 of `CHANGELOG.md`, so git must conflict, and each conflict
costs a full LLM run through the merge-conflict resolver. One fragment file
per PR makes the conflict impossible by construction: two PRs never touch the
same path.

### A) When an entry is required

Add exactly one fragment when the PR changes **observable behaviour**:

- new or changed CLI flags, env vars, repo vars, workflow inputs/outputs;
- new or changed workflows, scheduled jobs, or supervisors;
- behaviour changes an operator or consumer repo would notice, including
  defaults, thresholds, retries, and failure modes;
- new or changed DB collections, indexes, or contracts (§10);
- security fixes and data-loss fixes;
- anything that changes what a consumer repo receives on the next
  `@stable` sync.

Do **not** add a fragment for: pure refactors with no behaviour change,
comment/typo/formatting-only edits, test-only changes, or changes to the
fragment you are already shipping in this PR.

If in doubt, add one — a redundant entry is cheap, a missing one is not.

### B) Where it goes

Create `changelog.d/<issue-or-pr>-<slug>.md`, e.g.
`changelog.d/3712-soft-deadline-finalization.md`. The number is the issue or
PR the work belongs to, the slug is a few kebab-case words. One fragment per
PR. Never reuse another PR's filename — the unique path is the whole point.

An optional first line names the section the entry belongs to:

```md
<!-- changelog: fixed -->
- **Headline sentence.** …
```

Valid values: `added`, `changed`, `deprecated`, `removed`, `fixed`,
`security`. The default is `added`. Everything after the marker is the entry
body, copied verbatim.

### C) How it reaches `CHANGELOG.md`

`scripts/assemble_changelog.py` folds fragments into `CHANGELOG.md` and
deletes them. It runs from automation only (§18.A/§18.B) — never by hand:

- **this repo** — at release time, in the `release` job of
  `.github/workflows/mark-stable.yml` and
  `.github/workflows/test-and-mark-stable.yml`;
- **consumer repos** — on the existing sync, in
  `.github/workflows/update_workflows.yml`, which already commits and pushes
  to the default branch (daily 04:00 UTC cron, plus the `@stable`
  `repository_dispatch`).

The assembler auto-detects the repo's layout, so both are supported with no
per-repo configuration and no repo has to convert its changelog history:
Keep a Changelog (`## [Unreleased]` with `### Added` / `### Changed` /
`### Fixed` subsections) and bare `## YYYY-MM-DD` date headings, newest
first. Insertion is purely additive — no existing changelog line is removed
or reflowed.

`.gitattributes` carries `CHANGELOG.md merge=union` as a backstop for direct
edits that slip through. It is a *textual* union with no idea what a changelog
entry is: it keeps both sides' lines and orders them by merge order rather
than intent, and it applies to real `git merge` invocations (including CI
merge replay), not to GitHub's server-side mergeability estimate. Treat it as
the net under the mechanism, never as the mechanism.

### D) Entry structure

Follow this order when the information exists:

1. **Headline** — 1–2 sentences naming what shipped and the main
   user-visible change.
2. **Lead paragraph** — 3–5 sentences. Lead with what changed for users or
   operators, not with implementation detail. Name the real workflow,
   command, file, issue, or path when it helps the reader.
3. **The numbers that matter** — a short table when the change has real
   measurable details (exact counts, limits, schedules, paths, issue
   numbers). Omit it when nothing measurable improves the entry.
4. **Audience closing** — a short "What this means for \<audience\>"
   paragraph making the operational takeaway explicit.
5. **For contributors** — a final subsection, only when contributor-facing
   detail would distract from the lead. Implementation notes, follow-up
   details, and operator-only caveats go here.

### E) Voice rules

- Keep the lead user-facing; put contributor-only detail at the bottom.
- Use real numbers, real filenames, real workflow names, and real labels
  when they matter. Keep claims concrete and verifiable.
- Prefer commas or periods where an em dash would only add drama.
- Avoid AI-generic filler: `delve`, `robust`, `comprehensive`, `nuanced`,
  `fundamental`, `Here's the kicker`, `The bottom line`.

### F) Do not do this

- Do not mention branch-internal version bumps unless they changed shipped
  behaviour.
- Do not narrate the PR's revision history.
- Do not post-hoc rationalize why the scope ended up where it did.
- Do not invent numbers, vague placeholders, or generic filenames.

### G) Template

```md
<!-- changelog: added -->
- <Headline sentence. Optional second sentence.>

<Lead paragraph of 3 to 5 sentences. Start with user or operator impact.
Include exact workflow names, paths, numbers, and filenames when they matter.>

| The numbers that matter | Value |
| --- | --- |
| <metric> | <real value> |

What this means for <audience>: <closing paragraph.>

### For contributors

<Optional contributor-only details that do not belong in the lead.>
```

PR review checks new fragments against this section's structure, voice
rules, and audience split.

---

## §21. Merged-PR Commit Guard (MANDATORY)

Full text (guard conditions, shell forms it parses, fail-open contract, API
budget, push-command details): `.claude/docs/s21-merged-pr-commit-guard.md`.

**Never commit or push onto a branch whose pull request has already
merged.** A merged PR cannot track new work: commits stacked on its history
land on a branch no open PR carries, so the work is stranded. Long-lived
sessions hit this when the PR merges mid-session.

### A) The rule

Before every `git commit` and `git push` in an interactive session, confirm
the current branch is not sitting on a merged PR. If it is:

1. Restart the branch from the latest default branch, **keeping the same
   branch name**:
   `git fetch origin <default> && git checkout -B <branch> origin/<default>`
2. Re-apply the pending work and commit it there.
3. Open a **new** pull request. The merged PR is not reusable, and §12.A's
   single-PR rule does not apply across a merge boundary.

If the branch carries unmerged commits beyond the merged history, rebase
them onto the new base rather than discarding them.

### B) Enforcement

`.claude/hooks/pr_merge_status_guard.py`, a `PreToolUse` hook in
`.claude/settings.json` on `Bash` and on the GitHub MCP push tools
(`mcp__github__push_files`, `mcp__github__create_or_update_file`), blocks a
Git write when a merged PR's head is in the branch's history and no open PR
exists for the branch. It clears itself once the branch is rebuilt from the
default branch, so no override flag is needed. It asks for confirmation when
it cannot tell where a Git write runs (`sudo git push`, `xargs git push`,
`eval`, `pushd`, and similar).

### C) Fail-open contract

With the GitHub API unreachable, the guard falls back to git history: it
blocks when history shows the branch is merged, asks on a push when history
is inconclusive, and allows a bare commit with a warning. The merged-PR check
is skipped on the default branch and when `CLAUDE_PR_MERGE_GUARD=off` is set
(default: unset, i.e. enabled); the API-write confirmation safeguard stays on.

### D) API-call budget

One REST call per guarded command (§15), cached for 300 seconds for bare
commits only; a push always uses a live lookup.

### E) Push Command Shape

- Write every push as one plain command with literal arguments:
  `git push -u origin <branch>`, or after a §21.A reset
  `git push --force-with-lease origin <branch>`.
- No `for`/`while` retry loops and no `$VAR` or `$(...)` in push arguments.
  Create the branch and commit in their own command before the push. After a
  network error, retry as a new call with the same plain command.
- Push exactly one plain branch name: never a second refspec, a `+` refspec
  or a `src:dst` refspec. The guard asks for every branch deletion and every
  push to the default branch.
- Delete files with their absolute path, in a call of their own, never
  chained after a `cd`. Scratchpad files need no cleanup.

---

## §22. DigitalOcean Access (MANDATORY)

Full text: `.claude/docs/s22-digitalocean-access.md`.

The session provides `DIGITALOCEAN_ACCESS_TOKEN`, a DigitalOcean API token.
This applies here and in every consumer repo. **Reads are self-serve,
mutations are ask-first.**

### A) Read Operations — Act, Do Not Ask

Pull DigitalOcean data yourself instead of asking the user (a carve-out from
§2): list and inspect Apps, Droplets, managed databases, volumes and load
balancers; read deployed env vars; fetch build, deploy and runtime logs; check
deployment status, alerts and metrics. Prefer `doctl`; otherwise
`curl -sS -H "Authorization: Bearer ${DIGITALOCEAN_ACCESS_TOKEN}" "https://api.digitalocean.com/v2/<endpoint>"`.

Never print the token or write it into files, PRs, comments or diagnostics.
If it is missing or the API returns 401/403, say so once and continue
without it; no retry loops, no asking the user to run calls.

### B) Provisioning & Mutations — ALWAYS Ask First

Never create, modify, resize, scale, migrate, power off or destroy a
resource, change an app's spec or env vars, force a rebuild or redeploy,
restore a backup, or rotate credentials without the §2 ask, even under §12.
Name the resource type, size, region and billing impact. After approval, do
it yourself (§18).

### C) Resource IDs Live in the Agents File

Look up IDs in the agents file's `## DigitalOcean resources` table first. If
one is missing, ask for it; once given, verify it with one read and record it
through an `agents.d/` fragment (§30) in the same PR. IDs are §6 identifiers:
never rewrite or remove one without the §2 ask.

---

## §23. GitHub Access (MANDATORY)

Full text (web-session proxy details, transport, token scopes, and the
`gh api` permission guard's rules): `.claude/docs/s23-github-access.md`.

The session provides `GH_TOKEN`, a GitHub Personal Access Token. This applies
here and in every consumer repo. **Reads are self-serve, routine repository
writes are self-serve, destructive and administrative writes are ask-first.**

### A) Read Operations — Act, Do Not Ask

Pull GitHub data yourself instead of asking the user (a carve-out from §2):
PRs, issues, reviews, timelines and labels; Actions runs, jobs, check runs
and logs (`gh run view --log <run-id> -R <owner>/<repo>`, `--log-failed`);
commits, diffs, branches, tags, releases and file contents; repo metadata.
Stay within the repos the work touches: this repo, the consumers in
`.github/ai/consumer_repos.json`, and repos the user names.

**Web sessions:** the agent proxy replaces the PAT with its own GitHub App
token. Only repositories attached to the session are reachable (attach one
with `add_repo`, `access: "push"` for writes); GraphQL and some Actions paths
such as repo variables return 403; `gh auth status` reports a good token as
invalid. PAT-only reach exists only in a local session. Never route around
the proxy.

### B) Routine Repository Writes — Act, Do Not Ask

- pushing commits to the session's own designated working branch;
- opening a pull request, updating its title/body, marking it ready for
  review, and pushing follow-up commits to it;
- pull request and issue comments, review-thread replies, and resolving
  review threads you have addressed;
- applying or removing `ai:*` and other workflow labels the pipelines expect,
  and unsubscribing from PR activity. Subscribing to PR activity is **not**
  a routine write: §25 forbids it outright, and a `PreToolUse` hook blocks
  the call.

§19, §21 and §25 still govern every body you post, every commit and push,
and what happens after a push.

### C) Destructive & Administrative Writes — ALWAYS Ask First

**Never perform these without asking first** in the §2 Q/A format, even
under §12's proactive PR-review scope:

- merging a pull request, enabling auto-merge, or overriding a failing
  required check;
- force-pushing or otherwise rewriting history on any branch other than the
  session's own working branch, and any push to a default or protected
  branch;
- deleting or renaming branches, tags, releases, issues, or repositories, and
  closing a PR or issue the session did not itself open;
- repository or organization administration: settings, secrets, variables,
  rulesets, webhooks, collaborators, visibility, transfer, archive;
- dispatching workflow runs (`gh workflow run`, `repository_dispatch`,
  re-running a workflow) that start a billed or unattended pipeline;
- any write to a repository outside the scope the session was given.

Name the exact repository, object and change, so the user approves a
concrete action. After approval, do it yourself (§18).

**Command-invoked dispatches are already approved.** When the user invokes a
command whose documented job is to dispatch a workflow (`/implement-plan-ai`,
`/apply-analysis`, and any successor that says so in its own file), that
invocation is the approval for the dispatch it describes. Likewise, the
user's go on a §31 plan approves opening the issues (or the orchestrator
dispatch) that plan names.

### D) Transport & Tool Precedence

1. Prefer the `mcp__github__*` tools for anything they cover.
2. Use `GH_TOKEN` when MCP is not enough (capability missing, 403, truncated
   response, repo not attached).
3. Prefer REST (`gh api repos/<owner>/<repo>/...`) over GraphQL-backed
   commands (`gh pr list`, `gh issue list`, `gh search`).
4. Shape `gh api` calls so the §23.H guard can approve them: type IDs
   literally into the endpoint path (no `$VAR`, and no `gh api` inside
   `$(...)`); put GET parameters in the URL or pass `-X GET -f ...`; keep
   `gh api` out of loops, heredocs and file redirects; edit PR titles/bodies
   and post or edit comments with the MCP tools, never with `--input`,
   `-F body=@<file>` or a heredoc-built body.

Pass `-R <owner>/<repo>` on every `gh` call that needs repo context. Check
auth with `gh api user --jq .login`, not `gh auth status`.

### E) Token Hygiene and Degradation (hard rules)

- Never echo, log or print the token; reference it only as `$GH_TOKEN`.
- Never write it into committed files, PR bodies, comments, commit messages
  or diagnostics. Redact it if a tool response contains it.
- If it is missing or a call returns 401/403, say so once, fall back to the
  `mcp__github__*` tools, and continue; no retry loops, no asking the user to
  run calls (§18).
- Never commit a workflow, script or hook that reads `GH_TOKEN` from the
  session environment.

### F) API-Call Budget

§15 applies to interactive `gh` and `curl` calls too: extend or batch
existing calls, and cache results you will need again.

### G) Relationship to `GH_PAT` and `GITHUB_TOKEN` (§6)

In Actions, `GH_TOKEN` is exported from `secrets.GH_PAT`; in a session it is
the session PAT. Same name, two environments: never unify them (a §6 change).
Session tooling keeps accepting `GITHUB_TOKEN` as a fallback and prefers
`GH_TOKEN` when both are set.

### H) Permission Guard for `gh api`

`.claude/hooks/gh_api_write_guard.py`, a `PreToolUse` hook on `Bash`,
decides whether a `gh api` call prompts: reads and §23.B routine writes pass,
every other write asks in every permission mode, and a jq command-line option
passed to `--jq` is denied. Do not add `gh api` ask rules to settings; they
would override the hook. The classification table and Auto-mode behaviour
are in the full text; `tests/test_gh_api_write_guard.py` covers them.

### I) Permission Prompt Reports

Retired on 2026-10-03. The session-based Claude automation was replaced by
the Claude CLI engine in the Actions pipelines
(docs/plans/replace-claude-sessions-with-cli-engine-plan.md). Section
number kept per §6.

---

## §24. Cloudflare Access (MANDATORY)

Full text: `.claude/docs/s24-cloudflare-access.md`.

The session provides two Cloudflare credentials, each a single
`<account_id>:<api_token>` string for managing Workers. This applies here and
in every consumer repo. **Reads are self-serve, Worker deploys the task calls
for are self-serve, destructive and account-level writes are ask-first.**

### A) Credential Selection & Parsing

The two vars are different accounts and are NOT interchangeable:

| Env var | Covers | Use for |
|---|---|---|
| `FUNTOKEN_IO_CF` | `funtoken.io` | Workers serving the funtoken.io website |
| `FT_GAMES_CF` | `ft.games`, `5m.fun` | Workers serving the ft.games and 5m.fun websites |

For any other site or zone, STOP and ask (§2). Split on the **first** colon
only, via env expansion:

```
CF_ACCOUNT_ID="${FUNTOKEN_IO_CF%%:*}"
CF_API_TOKEN="${FUNTOKEN_IO_CF#*:}"
```

Prefer `wrangler` (export the halves as `CLOUDFLARE_ACCOUNT_ID` and
`CLOUDFLARE_API_TOKEN`); otherwise use the REST API. These are account-owned
tokens, so `/user/tokens/verify` returns 401 even when they work: probe with
`GET /client/v4/accounts/${CF_ACCOUNT_ID}/workers/scripts` instead.

### B) Read Operations — Act, Do Not Ask

Pull Cloudflare data yourself with the matching credential (a carve-out from
§2): Workers and their script, settings, bindings, routes, domains and cron
triggers; deployments and versions; zone and DNS records of covered domains;
bound KV, R2 and D1 resources; `wrangler tail` logs and analytics.

### C) Worker Deploys & Edits — Self-Serve When the Task Calls for Them

Creating and editing Workers is what these credentials exist for. When the
user's task is to build, fix, or update a Worker for a covered site,
perform the deploy yourself — do not hand the user a command to run (§18)
and do not add an extra approval round for the deploy the task already
implies. Self-serve writes include:

- creating a new Worker or uploading a new version of an existing Worker
  script for a covered site;
- editing a Worker's bindings, environment variables (non-secret), routes
  on covered zones, and cron triggers, when the task requires it;
- rolling back a deployment this session itself made that turns out to be
  broken.

Constraints that ride along:

- **Only for the covered domains.** A credential is scoped to its account;
  never use it to touch Workers or zones unrelated to the task.
- **Never execute unmerged code with credentials.** Project checks, package
  install lifecycle scripts, builds, and `wrangler deploy --dry-run` can
  execute repository code. Never run them from an unmerged PR or branch in
  a shell holding session credentials, even before deploy approval.
- **Deploy only a verified default-branch commit.** Require the target
  repository's default branch to be protected. Resolve its commit SHA via
  the GitHub API, fetch it, and deploy from a clean detached worktree pinned
  to that SHA, not a PR checkout or moving ref. Verify the worktree HEAD and
  cleanliness and recheck the API branch tip and protection immediately
  before deploying. If any check fails or cannot be verified, block the
  deploy; do not offer a manual Worker deploy as a workaround.
- **Validate without credentials.** Prefer GitHub check-runs for the pinned
  SHA; do not deploy while checks are failing or pending. Run local checks
  (typecheck, tests, `wrangler deploy --dry-run` where available) only in a
  credential-free, no-egress sandbox with an isolated home and no host
  credentials or Docker socket mounted. If isolation is unavailable, skip
  local checks and rely on check-runs; never run them in the credential-
  bearing session shell.
- **Limit deploy credential exposure.** Deploy only from the verified
  worktree, matching the target Worker against the repo's §24.F registry.
  Give the deploy process only the matching site's Cloudflare account ID and
  API token, with a clean home and unrelated session credentials stripped.
  These account-owned credentials are not Worker-scoped; provisioning a
  narrower token is an operator task, not a reason to expose additional
  credentials during deployment.
- **Preserve rollback.** Prefer versioned uploads/gradual rollouts where
  the account supports them; never delete the previous version as part of
  a deploy.

### D) Destructive & Account-Level Writes — ALWAYS Ask First

**Never perform these without asking first** in the §2 Q/A format — even
when the need seems obvious, and even under §12's proactive PR-review
scope (this subsection is NOT superseded by §12):

- deleting a Worker, route, custom domain, or cron trigger;
- creating, modifying, or deleting DNS records, or changing zone settings
  (SSL/TLS mode, caching rules, WAF, redirects);
- deleting or wiping KV namespaces, R2 buckets, or D1 databases, and any
  bulk data deletion inside them;
- setting or rotating Worker secrets (`wrangler secret put`), or rotating
  the API tokens themselves;
- account-level configuration: members, billing, subscriptions, zone
  additions/removals.

The question must name the exact account (which env var), zone, Worker,
and object, and what the operation changes, so the user approves a
concrete action rather than an intention. After approval, perform the
operation yourself with the credential — do not hand the user a command
to run (§18).

### E) Token Hygiene and Degradation (hard rules)

- Never echo, log or print the credential or either half; reference them
  only via env expansion. Never write them into files, PRs, comments, commit
  messages or diagnostics, and redact them from tool output.
- If a var is missing or the API returns 401/403, say so once, name the
  credential that failed, and continue; no retry loops, no asking the user to
  run calls (§18).

### F) Worker & Zone Identifiers Live in the Agents File

Look up Worker names, zone IDs, routes and namespace IDs in the agents
file's `## Cloudflare resources` table first (zone IDs can also be read via
§24.B). If one is missing, ask; once given, verify it with one read and
record it through an `agents.d/` fragment (§30) in the same PR. Entries are
§6 identifiers.

### G) Interactive Sessions Only

`FUNTOKEN_IO_CF` and `FT_GAMES_CF` are session env vars. Never commit a
workflow, script or hook that reads either one; Actions-side Cloudflare work
needs its own repo secret and review (§2). The unattended pipelines never see
this file.

---

## §25. PR Watching Is Disabled (MANDATORY)

Interactive Claude Code sessions **never subscribe to a pull request's
activity after pushing it**, and never act on a PR's CI or review activity
unprompted. This section applies in this repo and in every consumer repo that
receives this file via the `@stable` sync. It is strict: it is NOT
superseded by §12 (PR Review Mode), and the subscription ban holds even
when the user asks for it. User-requested scheduled checks are distinct
from subscriptions (§25.C).

### A) What is forbidden

- **Subscribing to PR activity** by any mechanism — the
  `subscribe_pr_activity` tool of any MCP server (`mcp__github__…`,
  `mcp__Claude_Code_Remote__…`, or any other), a harness "watch this PR"
  option, or any equivalent.
- **Offering to watch.** The harness's default prompt tells Claude to ask,
  after opening a PR, whether the user wants it watched for CI failures and
  review comments. Do not ask that question, and do not mention the option.
- **Entering autofix CI / address-comments mode on an event.** No
  `subscribe_pr_activity` wake-up is ever acted on (§12.G is inactive).
  Fixing CI or addressing review comments happens only when the user asks
  for it directly in the session (including through a scheduled status
  check they asked for, §25.C), and then under plain §12. A `claude/*`
  pull request is reviewed and fixed by the Actions review pipeline like
  any other PR.

### B) If the user asks for it anyway

Refuse requests to subscribe to PR activity, even if phrased as "watch this
PR". Reply in one or two sentences that CLAUDE.md §25 forbids PR-activity
subscriptions in this repository and that enabling them requires changing
§25 and removing the `pr_watch_guard.py` hook from `.claude/settings.json`
in a reviewed change first. Then continue with the rest of the task. Do not
work around the rule with another mechanism that rebuilds the subscription's
event-driven autofix on your own initiative: never set up a poll of a PR's CI or review
activity that the user did not ask for, and never hand one to a subagent
or another session. A scheduled status check the user asked for (§25.C)
is not a workaround.

### C) What is still allowed

- Pushing the branch, opening the PR, and reporting its link (§23.B).
- Unsubscribing from PR activity (`unsubscribe_pr_activity`) — tearing an
  existing subscription down is always permitted.
- Reading a PR's state, CI status, or review comments when the user asks
  about it, and acting on that request under §12 when asked to.
- Scheduled self check-ins and reminders (`send_later`, Routines) for work
  the user asked for. This includes a scheduled status check of a pull
  request when the user asks for one in the session (for example, "check
  hourly and get these PRs to completion"). Each check reads the PR's state,
  CI, and reviews as needed. A request to check status alone authorizes no
  fixes or workflow dispatches; act only within what the user asked for and
  under plain §12 (including §23.C's approval requirement for dispatches).
  Never arm one on your own initiative. Stop it when the PR merges or closes,
  or when the user says to stop. This section bans the PR-activity
  subscription, not the scheduler.

### D) Enforcement

`.claude/hooks/pr_watch_guard.py`, a `PreToolUse` hook in
`.claude/settings.json` under the matcher `mcp__.*__subscribe_pr_activity`,
blocks every `subscribe_pr_activity` call from any MCP server and never
blocks `unsubscribe_pr_activity` or the scheduled checks of §25.C. There is
deliberately no environment-variable escape hatch.
`tests/test_pr_watch_guard.py` covers the rule and the wiring; its fail-open
behaviour is in `.claude/docs/s25-pr-watching-disabled.md`.

### E) Interactive Sessions Only

The unattended pipelines read `unattended_system_instructions.md` and never
see this file. §25 says nothing about the unattended `review_autofix`
workflow (`Codex PR Self-Healing Semantic Agent`) or the orchestrator's
stall recovery — those keep their own policies.

---

## §26. Post-Push PR Status Check-In (MANDATORY)

Retired on 2026-10-03. The session-based Claude automation was replaced by
the Claude CLI engine in the Actions pipelines
(docs/plans/replace-claude-sessions-with-cli-engine-plan.md). Section
number kept per §6.

---

## §27. Workflow File Size Limit (MANDATORY)

This section applies in this repo and in every consumer repo that
receives this file via the `@stable` sync, and it is not superseded by §12.

GitHub does not start runs for a workflow file over **512,000 bytes**
(500 KiB), and it reports no error: every push instead gets a zero-job
`failure` run named after the file path ("workflow file issue"), and a
reusable workflow over the limit cannot be called.

- **Split at 480,000 bytes.** When a change leaves any
  `.github/workflows/*.yml` at or above 480,000 bytes, move the largest
  inline `run:` bodies into `scripts/` **in the same PR** until the file is
  well under that mark (aim for 50,000+ bytes of headroom). Check with
  `wc -c` whenever you grow a workflow file.
- **Never** raise the guard, and never get under it by splitting one
  workflow into several workflow files or by deleting comments that carry
  incident context.
- Moved bodies keep their behaviour: keep the step's `name:`, `id:`, `if:`,
  `env:` and `continue-on-error:` in the workflow (§6), move the body
  verbatim, and pass every `${{ }}` value through `env:` first, because
  GitHub does not substitute expressions inside a script file.
- In coding-workflows, `tests/test_workflow_file_size_limit.py` enforces the
  480,000-byte guard in CI, and `agents.md` ("Workflow file size limit")
  documents the `review_autofix.yml` pattern: `review_autofix_step_<slug>.sh`
  under `scripts/`, the resolving wrapper that sources it, the
  `REQUIRED_BOOTSTRAP_SCRIPTS` entry, and the test registry.

---

## §28. Unattended Auto-Decisions in `/implement-plan-claude` Projects (MANDATORY)

Retired on 2026-10-03. The session-based Claude automation was replaced by
the Claude CLI engine in the Actions pipelines
(docs/plans/replace-claude-sessions-with-cli-engine-plan.md). Section
number kept per §6.

---

## §29. OpenRouter Access (MANDATORY)

Full text (endpoint fields, attribution limits): `.claude/docs/s29-openrouter-access.md`.

The session provides `OR_MGMT_KEY`, an OpenRouter **management** key: it
reads and manages the account and its keys but cannot run models. It is not
the `OPENROUTER_API_KEY` inference secret the workflows use (§6). This applies
here and in every consumer repo. **Reads are self-serve, key and account
mutations are ask-first.**

### A) Read Operations — Act, Do Not Ask

Whenever a task needs OpenRouter spend, token usage, per-model cost, per-key
spend or remaining credit, pull it yourself (a carve-out from §2) instead of
asking, estimating from list prices, or rebuilding it from logs:
`GET https://openrouter.ai/api/v1/activity` (per day × model, last 30
completed UTC days), `/api/v1/keys` (per-key USD totals), `/api/v1/credits`,
with `Authorization: Bearer ${OR_MGMT_KEY}`.

- Older than 30 days is not available from the API; use saved reports.
- `/activity` is account-wide and split by model, not by key or repo:
  attribute cost to a pipeline role only when that role is the sole user of
  the model slug, and say when a slug is shared.
- Write downloaded responses under the session scratchpad, not the repo.

### B) Key & Account Mutations — ALWAYS Ask First

Never create, delete, disable, re-enable or rename a key, change a key's
spend limit, buy credits, or change billing or organization settings without
the §2 ask, even under §12. Name the key by `name` and `label`, never its
value. After approval, do it yourself (§18).

### C) Token Hygiene and Degradation (hard rules)

- Never echo, log or print `OR_MGMT_KEY` or a newly created key's value, and
  never write either into files, PRs, comments, commit messages or
  diagnostics.
- If the key is missing or the API returns 401/403, say so once and
  continue; no retry loops, no asking the user to run calls (§18).

### D) Interactive Sessions Only

`OR_MGMT_KEY` is a session env var. Never commit a workflow, script or hook
that reads it; Actions-side spend reporting needs its own secret and review
(§2). The unattended pipelines never see this file.

---

## §30. agents.md Fragments (MANDATORY)

**Never add to `agents.md` (or `AGENTS.md`) directly.** Write a fragment
instead. About a third of all commits touch `agents.md`, mostly the same few
places (the workflow architecture narrative and the two log-prefix
registries), so concurrently open PRs conflict there and hold the merge train.
One fragment file per PR makes those conflicts impossible: two PRs never touch
the same path. This mirrors §20 for `CHANGELOG.md`.

### A) Where it goes

Create `agents.d/<issue-or-pr>-<slug>.md`, for example
`agents.d/3712-soft-deadline-finalization.md`. Each block starts with a marker naming an
existing `## ` heading of the agents file exactly as written there; the text
after it is copied verbatim to the end of that section:

```md
<!-- agents: section="Workflow architecture" -->
New paragraph, bullets or table rows.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `NEW_PREFIX` (`scripts/x.sh`: `outcome=`)
LOG_PREFIX.name=NEW_PREFIX
```

- One fragment may hold several blocks.
- In `## Stable log prefixes (contractual)`, `` - `PREFIX` `` bullets land
  after the last registry bullet and `LOG_PREFIX.name=` lines after the last
  name line, so both registries stay contiguous.
- A table row or list item continues the table or list it follows.
- Add ` new` to create a section (`<!-- agents: section="Topic" new -->`).
  Without it an unknown heading fails CI, so a typo cannot create a stray
  section.
- Never target `## Repo-tree (auto-generated)`; `make generate` owns it.
- A scope (`files_touched`, `ai:scope:`) that covers `agents.md` or
  `AGENTS.md` covers its top-level `agents.d/*.md` fragments.

### B) What still edits the agents file directly

- `make generate` (the TREE blocks);
- correcting or removing existing text, which fragments cannot do. Keep such
  edits small and separate from new documentation.

### C) How it reaches the agents file

`scripts/assemble_agents.py` folds fragments in filename order and deletes
them. It runs from automation only (§18.A/§18.B), never by hand:

- **this repo**: in the release job's "Assemble changelog fragments" step of
  `.github/workflows/mark-stable.yml` and
  `.github/workflows/test-and-mark-stable.yml`, in the same commit as the
  changelog fold;
- **consumer repos**: on the existing sync, in
  `.github/workflows/update_workflows.yml` (step "Assemble agents.md
  fragments").

CI runs `python3 scripts/assemble_agents.py check --repo-root .` and fails a
fragment that would not fold. Readers see pending fragments: the static
context (`scripts/build_static_context.sh`), the materiality check and the
tests read the agents file through `assemble_agents.py render` or
`tests/agents_doc.py`.

---

## §31. Plan, Then Hand Off to the Pipeline (MANDATORY)

In an interactive session, Claude implements only small fixes itself. For
any other task it clarifies, writes the plan, and hands the plan to the
unattended pipeline by opening issues (or dispatching the orchestrator). This
applies in this repo and in every consumer repo that receives this file via
the `@stable` sync.

### A) Small fixes: implement in the session

A task is small only when all of these hold:

- it changes at most two files, not counting a test for the fix and the §20
  / §30 fragments;
- it adds or changes no env var, repo variable, workflow, scheduled job, DB
  collection, index or contract, and no public interface;
- it needs no plan: the cause and the fix are clear (a typo, a
  one-function bug, a config value).

If it is unclear whether a task is small, it is not small. This section does
not change:

- tasks the user explicitly asks to have implemented in the session;
- PR review fixes under §12 (they land in the PR under review, §12.A);
- slash commands whose own file says what they ship (for example
  `/investigate-issue`, `/analyze-log`);
- questions, investigations and other work that changes no code.

### B) Everything else: plan, approve, hand off

1. **Clarify** per §2 until no question is open, after reading the context
   the PRE-TASK rule names.
2. **Plan.** Write the plan with the structure `/write-plan` uses, and add
   a §18.E section to it (that structure does not list one).
   - Work that fits in one PR: the plan becomes the issue body.
   - Work that needs several independently-mergeable PRs: follow
     `/write-plan` (a plan PR under `docs/plans/`); that plan PR is the
     plan the user reviews in step 3. After the go, hand it off with
     `/implement-plan-claude`.
3. **Show the plan and wait for the user's go.** Open nothing else before
   it: no issue, no `/implement-plan-claude` dispatch, no implementation PR.
   The go is the approval §23.C requires for opening the issues or
   dispatching the orchestrator.
4. **Open the issues** (single-PR path only; the multi-PR path ends at the
   `/implement-plan-claude` dispatch, and the orchestrator opens its own
   tracking and wave issues). One issue per repository that needs a change,
   labelled `ai:engine-claude` at creation, cross-linked with
   `Refs <owner>/<repo>#<N>` (never auto-close keywords, §19). Check that each
   target repository runs the pipeline (this repo, or a consumer in
   `.github/ai/consumer_repos.json`) and attach it with `add_repo` when
   needed; if it does not run the pipeline, ask. An issue opened by the user's
   account starts clarify on its own; do not also post `/reclarify`.
5. **Report** each issue or dispatch link. Do not implement the planned work
   in the session: no branch, no code edits, no PR for it.

---

## FINAL REMINDER

If uncertainty exists: **ASK (multiple-choice). DO NOT EXECUTE.**

Accuracy > speed. Safety > convenience. Backward compatibility is mandatory.
