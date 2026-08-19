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
- `agents.md` (or `AGENTS.md` — whichever casing the repo root has; same file,
  read it every session)
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

**Never commit or push onto a branch whose pull request has already
merged.** A merged PR is finished — it cannot track new work. Commits
stacked on top of already-merged history land on a branch no open PR
carries, so the work is stranded: it never reaches the default branch and
is lost when the branch is deleted.

This is a real failure mode for long-lived interactive sessions. A session
that runs for days or weeks outlives the PR it opened; the PR merges
mid-session and the model, working from context that predates the merge,
keeps committing to the same branch.

### A) The rule

Before every `git commit` and `git push` in an interactive session, confirm
the current branch is not sitting on a merged PR. If it is:

1. Restart the branch from the latest default branch, **keeping the same
   branch name**:
   `git fetch origin <default> && git checkout -B <branch> origin/<default>`
2. Re-apply the pending work and commit it there.
3. Open a **new** pull request. The merged PR is not reusable, and §12.A's
   single-PR rule does not apply across a merge boundary — the merged PR is
   closed history, so the follow-up work is a new PR, not a split of an
   existing one.

If the branch carries unmerged commits beyond the merged history, rebase
them onto the new base rather than discarding them.

### B) Enforcement

The rule is enforced deterministically by `.claude/hooks/pr_merge_status_guard.py`,
wired as a `PreToolUse` hook on `Bash` in `.claude/settings.json`. Prose alone
cannot enforce this — the instruction is furthest from the context window's live
edge exactly when the session has run long enough for the merge to happen.

The guard blocks a command only when all three conditions hold:

1. A **merged** PR exists whose head ref is the current branch, **and**
2. no **open** PR exists for the current branch, **and**
3. that merged PR's head commit is an ancestor of `HEAD`.

Condition 3 is what makes the guard self-clearing. Branch names are reused
after the reset in §21.A, so the merged PR matches `--head <branch>`
forever; ancestry is what separates "stacking on merged history" from
"fresh work that reuses the name". No override flag is needed, and the
guard goes quiet as soon as the branch is reset — before the new PR exists.

### C) Fail-open contract

Any inability to answer the question — `gh` missing, token expired or
unauthenticated, network failure, unparseable hook payload, detached HEAD,
not a git repo, undeterminable `<owner>/<repo>` — **allows** the command and
emits a warning naming the branch. A guard that hard-blocks every commit
whenever a token lapses would cost more than the bug it prevents.

The guard is skipped entirely on the default branch, and when
`CLAUDE_PR_MERGE_GUARD=off` is set (default: unset, i.e. enabled).

### D) API-call budget

Per §15, the guard issues **one** API call per guarded command — a single
`state=all` request answers both the merged and the open question — and
caches the result for 300 seconds keyed on `<slug>/<branch>`. Cached data may
satisfy an *allow*; a *block* is always re-verified against a live call first,
so opening a new PR clears the guard immediately instead of after the TTL.

The call goes over REST (`gh api repos/<slug>/pulls`), **not** `gh pr list`.
Claude Code Web's agent proxy serves only a pinned set of GraphQL operations
and rejects the rest with HTTP 403, and `gh pr list` is GraphQL-backed — using
it as the primary transport would make the guard fail open on every commit in
exactly the long-running web sessions §21 exists to protect. `gh pr list`
remains wired as a transport fallback for environments where REST is gated
instead. The fallback is a retry of the same question on a different
transport, never a second query.

---

## §22. DigitalOcean Access (MANDATORY)

The session environment provides a `DIGITALOCEAN_ACCESS_TOKEN` env var — a
DigitalOcean API token. This section applies in this repo and in every
consumer repo that receives this file via the `@stable` sync. It splits
DigitalOcean operations into two postures: **reads are self-serve** (act,
do not ask), **mutations are ask-first** (always confirm before acting).

### A) Read Operations — Act, Do Not Ask

When a task needs data from DigitalOcean, pull it yourself with the token
instead of asking the user to fetch it or to run commands on your behalf.
This is an explicit carve-out from §2 for **read-only** DigitalOcean calls —
needing DO-hosted data is not, by itself, a reason to stop and ask.
Self-serve reads include (non-exhaustively):

- listing and inspecting Apps, Droplets, managed databases, volumes, and
  load balancers, and their specs/configuration;
- reading deployed app-level env vars to verify that a name or value
  matches what the code and docs expect;
- fetching build, deploy, and runtime logs to diagnose failures;
- checking deployment status, alert policies, and monitoring/metrics data.

Transport: prefer `doctl` when installed (it honours the
`DIGITALOCEAN_ACCESS_TOKEN` env var natively); otherwise call the REST API
directly:

```
curl -sS -H "Authorization: Bearer ${DIGITALOCEAN_ACCESS_TOKEN}" \
  "https://api.digitalocean.com/v2/<endpoint>"
```

Token hygiene (hard rules):
- Never echo, log, or print the token value; reference it only via env
  expansion (`$DIGITALOCEAN_ACCESS_TOKEN`).
- Never write the token into committed files, PR bodies, issue comments,
  or diagnostic output. Redact it if a tool response contains it.
- If the token is missing or the API returns 401/403, report that to the
  user and continue with what can be done without it — do not retry-loop
  and do not ask the user to run the API calls manually.

### B) Provisioning & Mutations — ALWAYS Ask First

**Never create, modify, resize, or destroy DigitalOcean resources without
asking first** in the §2 Q/A format — even when the need seems obvious,
and even under §12's proactive PR-review scope (this subsection is NOT
superseded by §12). Ask-first operations include (non-exhaustively):

- spinning up new Droplets, Apps, managed databases, volumes, load
  balancers, or any other billable resource;
- resizing, scaling, or migrating existing resources;
- destroying or powering off resources;
- changing a deployed app's spec or env vars, forcing rebuilds/redeploys,
  restoring from backups, or rotating credentials.

The question must name the exact resource type, size/plan, region, and
estimated billing impact where known, so the user approves a concrete
action rather than an intention. After approval, perform the operation
yourself with the token — do not hand the user a command to run (§18).

### C) Resource IDs Live in the Agents File

Each repo's root agents file (`agents.md` in this repo; `AGENTS.md` in
consumer repos that use that casing) carries a `## DigitalOcean resources`
section listing the App / database / Droplet IDs relevant to that repo,
as a table:

```md
## DigitalOcean resources

| Resource | Type | ID | Notes |
|---|---|---|---|
| <name> | app / db / droplet / ... | <id> | <role, environment> |
```

Rules:
- **Look there first.** Before asking for any DigitalOcean resource ID,
  read the repo's agents file. Use recorded IDs without re-asking.
- **Ask when missing.** If a needed ID is not recorded, ask the user for
  it (§2 Q/A format, free-text answer allowed for the ID itself).
- **Save once provided.** After the user supplies an ID, verify it
  resolves with one read API call (§22.A), then add it to the
  `## DigitalOcean resources` section of the agents file **in the same
  PR/commit as the work that needed it**, so it is never asked for again.
  Create the section if the file lacks it.
- IDs are identifiers under §6 — never remove or rewrite an existing
  entry without the §2 ask flow; correcting a stale ID requires telling
  the user what changed.

---

## §23. GitHub Access (MANDATORY)

The session environment provides a `GH_TOKEN` env var — a GitHub Personal
Access Token whose reach is wider than the session's built-in GitHub tooling
(other repositories, Actions logs, endpoints the MCP surface does not expose).
This section applies in this repo and in every consumer repo that receives
this file via the `@stable` sync. It splits GitHub work into three postures:
**reads are self-serve** (act, do not ask), **routine repository writes are
self-serve**, **destructive and administrative writes are ask-first**.

`GH_TOKEN` is the interactive counterpart of `DIGITALOCEAN_ACCESS_TOKEN`
(§22): a standing credential the session uses itself, rather than a reason to
hand the user commands to run.

### A) Read Operations — Act, Do Not Ask

When a task needs data from GitHub that the session's default tooling cannot
reach, pull it yourself with the token instead of asking the user to fetch it
or to run `gh` on your behalf. This is an explicit carve-out from §2 for
**read-only** GitHub calls — needing GitHub-hosted data is not, by itself, a
reason to stop and ask. Self-serve reads include (non-exhaustively):

- pull requests, issues, review threads, review comments, timeline events,
  labels, and linked-issue relationships;
- Actions workflow runs, jobs, check runs, annotations, and **run/job logs**
  (`gh run view --log <run-id> -R <owner>/<repo>`,
  `gh run view --log --job <job-id> -R <owner>/<repo>`,
  `gh run view --log-failed <run-id> -R <owner>/<repo>`,
  `gh api repos/<owner>/<repo>/actions/runs/<id>/logs`);
- commits, diffs, blame, branches, tags, releases, and file contents at any
  ref, including in a consumer repo whose wrapper or workflow is implicated;
- repo and org metadata a task depends on: default branch, protection rules
  as reported by the API, repo variables (`gh variable list`), workflow
  definitions, and `gh api rate_limit`.

**Repo scope is not widened by the token.** A PAT that *can* read a
repository is not permission to browse one the task does not involve. Stay
within the repos the work actually touches: this repo, the consumers listed
in `.github/ai/consumer_repos.json`, and any repo the user names. When the
host session exposes a repo-attachment mechanism, prefer attaching the repo
over reaching around the session's declared scope.

### B) Routine Repository Writes — Act, Do Not Ask

These writes are ordinary session work, already implied by the task the user
gave you. Perform them with the token (or the MCP equivalent, per §23.D)
without a separate approval round:

- pushing commits to the session's own designated working branch;
- opening a pull request, updating its title/body, marking it ready for
  review, and pushing follow-up commits to it;
- pull request and issue comments, review-thread replies, and resolving
  review threads you have addressed;
- applying or removing `ai:*` and other workflow labels the pipelines expect,
  and subscribing/unsubscribing to PR activity.

Two constraints ride along and are **not** relaxed by this subsection:
§19 (no auto-close keywords against `ai:orchestrator-tracking` issues) governs
every body you post, and §21 (merged-PR commit guard) governs every commit
and push.

### C) Destructive & Administrative Writes — ALWAYS Ask First

**Never perform these without asking first** in the §2 Q/A format — even when
the need seems obvious, and even under §12's proactive PR-review scope (this
subsection is NOT superseded by §12):

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

The question must name the exact repository, the exact object (PR number,
branch, setting), and what the operation changes, so the user approves a
concrete action rather than an intention. After approval, perform the
operation yourself with the token — do not hand the user a command to run
(§18).

**Command-invoked dispatches are already approved.** When the user invokes a
command whose documented job is to dispatch a workflow — `/implement-plan-ai`,
`/apply-analysis`, and any successor that says so in its own file — that
invocation *is* the approval for the dispatch the command describes. Do not
re-ask; the ask-first rule above covers dispatches you would be initiating on
your own judgement.

### D) Transport & Tool Precedence

1. **Prefer the `mcp__github__*` tools** for anything they cover. They are
   scope-checked by the host session and keep the audit trail consistent.
2. **Reach for `GH_TOKEN` when MCP is not enough** — the capability is not
   exposed, the call is scope-gated (403), the response is truncated, or the
   repository is not attached to the session.
3. **Prefer REST over GraphQL** on the `gh` path. Claude Code Web's agent
   proxy serves only a pinned set of GraphQL operations and rejects the rest
   with HTTP 403, so GraphQL-backed commands (`gh pr list`, `gh issue list`,
   `gh search`) can fail in exactly the environments this section is written
   for. Use `gh api repos/<owner>/<repo>/...` as the primary transport and
   treat the GraphQL-backed command as a fallback, never the reverse — the
   same reasoning §21.D applies to the merged-PR guard.

Transport, in order of preference:

```
gh api repos/<owner>/<repo>/<endpoint>          # gh reads GH_TOKEN natively
curl -sS -H "Authorization: Bearer ${GH_TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/<owner>/<repo>/<endpoint>"
```

Two environment facts that make `gh` calls fail in confusing ways:

- **Pass `-R <owner>/<repo>` on every `gh` call that needs repo context.** In
  Claude Code Web the only git remote points at a local proxy, so bare calls
  fail with `failed to determine base repo` — which is not an auth problem.
  The SessionStart hook prints the resolved slug.
- **Check auth directly, nounset-safe**, rather than inferring it from the
  SessionStart log:
  `{ [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; } && gh auth status`.
  The hook's secondary probe only tests `actions:read` for one repo and can
  emit a `NOTE`/`WARNING` while `gh` works fine for everything else.

Useful token scopes: classic PATs need `repo` plus `workflow` (and
`read:org` for org metadata); fine-grained PATs need contents, pull requests,
issues, and **actions: read** for run/job logs. A token without `actions:read`
still serves every non-Actions read in §23.A.

### E) Token Hygiene and Degradation (hard rules)

- Never echo, log, or print the token value; reference it only via env
  expansion (`$GH_TOKEN`).
- Never write the token into committed files, PR bodies, issue comments, commit
  messages, or diagnostic output. Redact it if a tool response contains it.
- If the token is missing, `gh auth status` reports it invalid, or the API
  returns 401/403, **say so once**, fall back to the `mcp__github__*` tools for
  whatever they can still reach, and continue with the rest of the task — do
  not retry-loop, and do not ask the user to run the calls manually (§18).
  A failing `gh auth status` when `GH_TOKEN` is set means the PAT is invalid,
  expired, or was saved incorrectly in the session environment; report that
  diagnosis rather than "gh is broken".
- Never commit a workflow, script, or hook that reads `GH_TOKEN` from the
  session environment. This section governs interactive sessions only;
  Actions-side code authenticates via `secrets.GH_PAT` (§23.G).

### F) API-Call Budget

§15 (GitHub API Call Hygiene) applies to interactive `gh` and `curl` calls,
not just to workflow code. Before adding a call, check whether an existing one
in the same task can be extended; batch per-item lookups; cache results you
will need again in the same session instead of re-fetching per loop iteration.

### G) Relationship to `GH_PAT` and `GITHUB_TOKEN` (§6)

`GH_TOKEN` already exists as an identifier in this repo's Actions workflows
and scripts, where it is exported from the `GH_PAT` secret. That name is
unchanged and must not be repurposed — the two readings are the same variable
name in two different environments:

| Environment | Value | Governed by |
|---|---|---|
| Actions workflows / unattended pipelines | `${{ secrets.GH_PAT }}` exported as `GH_TOKEN` | workflow YAML, `unattended_system_instructions.md` |
| Interactive Claude Code session | PAT set in the Claude Code cloud session environment | this section |

Consequences:

- Do not "unify" the two by rewriting workflow YAML to read a session env var,
  and do not add `GH_PAT` handling to interactive tooling. Either direction is
  a §6 breaking change and requires the §2 ask flow.
- `GITHUB_TOKEN` remains the accepted fallback name in session tooling that
  already checks both (`.claude/hooks/session-start.sh`, the `.claude/commands/*`
  Tool Access blocks). Keep accepting both; prefer `GH_TOKEN` when both are set.
- The unattended pipelines read `unattended_system_instructions.md` and never
  see this file, so §23 grants no new access to any codex-driven phase.

---

## §24. Cloudflare Access (MANDATORY)

The session environment provides two Cloudflare credential env vars for
managing Cloudflare Workers. Each value is a single string in the format
`<account_id>:<api_token>` — the Cloudflare account ID, a literal colon,
then an API token authorized to create and edit Workers in that account.
This section applies in this repo and in every consumer repo that receives
this file via the `@stable` sync. It follows the same posture split as §22
and §23: **reads are self-serve**, **Worker deploys the task calls for are
self-serve**, **destructive and account-level writes are ask-first**.

### A) Credential Selection & Parsing

Pick the credential by the site the work targets — the two vars are
different Cloudflare accounts and are NOT interchangeable:

| Env var | Covers | Use for |
|---|---|---|
| `FUNTOKEN_IO_CF` | `funtoken.io` | Workers serving the funtoken.io website |
| `FT_GAMES_CF` | `ft.games`, `5m.fun` | Workers serving the ft.games and 5m.fun websites |

If the target site or zone is not one of the domains above, neither
credential covers it — STOP and ask (§2 Q/A format) rather than guessing
which account to use.

Split on the **first** colon only (API tokens can theoretically contain
further separators; account IDs are 32 hex chars and never contain `:`),
referencing the value only via env expansion:

```
CF_ACCOUNT_ID="${FUNTOKEN_IO_CF%%:*}"
CF_API_TOKEN="${FUNTOKEN_IO_CF#*:}"
```

Transport: prefer `wrangler` when installed — export the split halves as
`CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`, which wrangler honours
natively. Otherwise call the REST API directly:

```
curl -sS -H "Authorization: Bearer ${CF_API_TOKEN}" \
  "https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/workers/scripts"
```

Health-check gotcha: these are **account-owned** tokens, so
`GET /client/v4/user/tokens/verify` returns 401 `Invalid API Token` for
them even when they work. Do not diagnose a credential as invalid from
that endpoint — probe with an account-scoped read instead (the
`workers/scripts` list above is the canonical probe).

### B) Read Operations — Act, Do Not Ask

When a task needs data from Cloudflare, pull it yourself with the matching
credential instead of asking the user to fetch it. This is an explicit
carve-out from §2 for **read-only** Cloudflare calls — needing
Cloudflare-hosted state is not, by itself, a reason to stop and ask.
Self-serve reads include (non-exhaustively):

- listing Workers and reading a Worker's script content, settings,
  bindings, routes, custom domains, and cron triggers;
- listing deployments/versions of a Worker and reading rollback state;
- reading zone and DNS records for the covered domains to verify routing;
- listing KV namespaces, R2 buckets, and D1 databases bound to a Worker
  (and reading individual values needed to diagnose a defect);
- tailing Worker logs (`wrangler tail`) and reading analytics/metrics to
  diagnose failures.

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
- **Validate before deploy.** Run the project's checks (typecheck, tests,
  `wrangler deploy --dry-run` where available) before uploading; a deploy
  is user-visible on a live site the moment it lands.
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

- Never echo, log, or print the credential or either of its halves;
  reference them only via env expansion (`$FUNTOKEN_IO_CF`,
  `$FT_GAMES_CF`, or split-out shell variables as in §24.A).
- Never write the credential into committed files, PR bodies, issue
  comments, commit messages, or diagnostic output. Redact it if a tool
  response contains it. The account ID half is less sensitive than the
  token half, but treat the combined value as a secret throughout.
- If a var is missing or the API returns 401/403, **say so once**, report
  which credential failed, and continue with what can be done without it —
  do not retry-loop, and do not ask the user to run the calls manually
  (§18).

### F) Worker & Zone Identifiers Live in the Agents File

Each repo's root agents file (`agents.md` in this repo; `AGENTS.md` in
consumer repos that use that casing) carries a `## Cloudflare resources`
section listing the Worker names, zone IDs, routes, and namespace IDs
relevant to that repo, as a table:

```md
## Cloudflare resources

| Resource | Type | ID / name | Credential | Notes |
|---|---|---|---|---|
| <name> | worker / zone / kv / r2 / d1 / route | <id or name> | FUNTOKEN_IO_CF / FT_GAMES_CF | <role, environment> |
```

Rules (same as §22.C):

- **Look there first.** Before asking for any Cloudflare identifier, read
  the repo's agents file. Use recorded entries without re-asking. Zone
  IDs are also discoverable self-serve via a §24.B read — prefer looking
  them up over asking.
- **Ask when missing.** If a needed identifier is neither recorded nor
  discoverable via a read, ask the user (§2 Q/A format, free-text answer
  allowed for the identifier itself).
- **Save once provided.** Verify a supplied identifier resolves with one
  read call (§24.B), then record it in the agents file **in the same
  PR/commit as the work that needed it**. Create the section if the file
  lacks it.
- Entries are identifiers under §6 — never remove or rewrite an existing
  entry without the §2 ask flow.

### G) Interactive Sessions Only

- `FUNTOKEN_IO_CF` and `FT_GAMES_CF` are session env vars, like
  `DIGITALOCEAN_ACCESS_TOKEN` (§22) and the session reading of `GH_TOKEN`
  (§23). No Actions workflow reads them, and none may be added that does:
  never commit a workflow, script, or hook that reads either var from the
  session environment. Actions-side Cloudflare work would need its own
  repo secret and its own review — route that through §2.
- The unattended pipelines read `unattended_system_instructions.md` and
  never see this file, so §24 grants no new access to any codex-driven
  phase.

---

## FINAL REMINDER

If uncertainty exists: **ASK (multiple-choice). DO NOT EXECUTE.**

Accuracy > speed. Safety > convenience. Backward compatibility is mandatory.
