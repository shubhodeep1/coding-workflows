# Codex System Instructions (Production Code + MongoDB)
## HARD ENFORCEMENT — READ BEFORE ANY ACTION

These instructions are **mandatory**.  
Codex must follow them **before doing anything in this repository**.

Failure to comply is considered a **blocking error**.

---

## PRE-TASK MANDATORY CONTEXT LOADING (CRITICAL)

**Before any task, analysis, plan, or execution, Codex MUST read:**
- `README.md`
- `agents.md`
- all `/db/contracts/*.yml` (or `.json`) files **relevant to the collections that may be touched**

This applies to:
- planning
- answering questions
- proposing changes
- writing code
- reviewing PRs
- suggesting fixes or improvements

If any of these files are missing, outdated, or unclear:
**STOP and ask clarifying questions using the mandatory multiple-choice format.**

Never assume behavior that is not explicitly documented.

---

## Serena (MCP) semantic tooling (STRONGLY PREFERRED)

Goal: reduce token usage + speed up code understanding/refactors by using Serena’s semantic tools instead of full-file reads.

Rules:
- Prefer Serena semantic tools for code navigation and reading over full-file reads/grep.
- Use Serena tools like:
  - `mcp__serena__get_symbols_overview`
  - `mcp__serena__find_symbol`
  - `mcp__serena__find_referencing_symbols`
  - `mcp__serena__search_for_pattern`
- Avoid reading entire source files unless absolutely necessary; read only the symbols/regions you need.
- If Serena tools are unavailable or failing, fall back to normal file reads and continue (do not stall).

## 0. Prime Directive (NON-NEGOTIABLE)

If you are **not 100% certain** that the outcome of your actions will match the user’s expectations:

**STOP. ASK QUESTIONS. DO NOT PROCEED.**

This rule applies **always**, even if:
- the task looks trivial
- the user did not use `[codex-plan]`
- the user did not use `/plan`
- similar patterns exist in the repo
- you believe the intent is “obvious”

No assumptions. Ever.

---

## 1. Core Priorities (Strict Order)

1. Security  
2. Correctness & safety  
3. Backward compatibility  
4. Operational clarity  
5. Performance  
6. Speed (last)

---

## 2. Always-On Ask-First Mode (CRITICAL)

Ambiguity is a **hard stop**.

Before drafting questions, Codex MUST:
- restate and validate understanding of the task objective
- study the repository thoroughly enough to avoid avoidable questions
- identify all currently-known blocking uncertainties

You MUST ask clarifying questions **before** writing or modifying:
- code
- schemas
- indexes
- configs
- scripts
- docs
- migrations
- infrastructure

### 2.0 Clarification Batching Rule (MANDATORY)

When clarification is required, Codex MUST ask **all known clarifying questions in a single batch**.

Follow-up questions are allowed **only** if answers to that first batch introduce new ambiguity that could not have been known earlier.

Codex MUST NOT drip-feed obvious questions across multiple messages.

### 2.1 Mandatory Decision Prompt Format (ANTI-MISALIGNMENT — CRITICAL)

When Codex needs **any** decision, clarification, preference, or approval from the user, it MUST:

- **NOT use numeric question numbering** (1, 2, 3, etc.)
- **NOT rely on markdown list numbering for questions**
- use **stable question identifiers**: `Q1`, `Q2`, `Q3`, …
- require **letter-only answers**: `A`, `B`, `C`, or `A+C`
- number NOTHING except answer options if absolutely needed

The user must be able to reply with **only the answer token(s)**  
(e.g. `A`, `B`, `A+C`) — no prefixes, no prose.

---

#### Required Question Format (MANDATORY)

Each decision block MUST follow this exact structure:

**Q<ID>: <short question text>**

Choices:
- **A** — <choice description>
- **B** — <choice description>
- **C** — <choice description>

Recommendation tagging rule (MANDATORY):
- For every multiple-choice question, mark one or more options with ` (RECOMMENDED)` based on Codex's best interpretation of the task.
- If multiple options are equally strong, mark each qualifying option as ` (RECOMMENDED)`.
- Never leave all options unmarked.

Reply format:
`Q<ID>: <LETTER(S)>`

Example reply:
`Q2: B`

---

#### Example (CORRECT)

**Q1: Which environment should this apply to?**

Choices:
- **A** — Production only
- **B** — Staging only
- **C** — Both production and staging
- **D** — Development only

Reply format:
`Q1: C`

---

#### Forbidden Formats (DO NOT USE)

- Numbered questions (`1)`, `2)`, `3)`)
- Mixed numbering (`3. A`, `4. B`)
- Markdown auto-numbered lists
- Inline questions inside paragraphs
- Multiple questions under a single ID

---

#### Multiple Decisions Rule

- Each decision = **one Q<ID>**
- Never bundle multiple decisions into one question
- If multiple selections are allowed, explicitly say so:
  > “You may select multiple letters”

---

#### When to Ask vs Proceed

- If **any ambiguity exists** → ask using this format and STOP
- If the user already answered the exact decision → proceed silently
- Never infer answers from context

---

**This format is designed to be:**
- unambiguous
- copy-paste safe
- markdown-proof
- fast to answer

### You must ask questions if ANY of the following are unclear:

#### Scope
- which repo/module/service/script
- runtime vs batch vs migration vs one-off
- prod/staging/dev applicability

#### Behavior
- exact expected behavior
- edge cases and failure handling
- safety and performance constraints

#### Interfaces
- API / CLI / env vars
- backward compatibility requirements
- logging and observability expectations

#### Data / MongoDB
- collections touched
- uniqueness rules (null / missing / empty)
- index contracts or rollout constraints

#### Operations
- execution timing
- concurrency expectations
- rollback or failure recovery

### Forbidden behavior
- guessing intent
- “reasonable defaults” without confirmation
- silent refactors or cleanups
- speculative fixes

If unsure: **ask using the mandatory multiple-choice format** — never choose silently.

---

## 3. Code Assumptions (Production-Bound)

Assume **all code is production-bound**.

Before outputting anything, verify:
- logic correctness
- error paths
- race conditions
- idempotency
- deployment safety

---

## 4. Environment Variables

- If you add a new env var, **always provide a default value**
- Do NOT introduce env vars without defaults unless explicitly instructed
- Preserve all existing env var names forever

---

## 5. Minimal Change Set Rule

- Do NOT change formats, data types, or unrelated logic
- Do NOT reformat files unless required to fix errors or integrate changes
- Do NOT create test scripts unless explicitly asked
- Extend existing mechanisms — never compete with them

---

## 6. Backward Compatibility / Naming Immutability (CRITICAL)

You must NEVER rename, remove, or repurpose existing identifiers without asking first and detailing what the removed items currently do, including:

- variables
- functions
- classes
- modules / files
- exported symbols
- CLI flags
- environment variables
- URL paths, query params, body fields
- JSON fields
- DB fields
- index names
- event names
- metric names
- log keys

All renames are **breaking changes**, even if “internal”.

### If a new name is required
- add it alongside the old one
- accept both old + new inputs
- preserve old outputs as canonical
- canonicalize only at boundaries
- document aliases and precedence
---

## 7. Output Requirements

In every final response:

- list all files changed
- list line ranges with **MAJOR logic changes**
- ignore formatting-only edits

If behavior changes:
- update `README.md` and/or `agents.md` with:
  - env vars
  - DB behavior
  - indexes
  - operational steps
  - failure modes

---

## 8. Debugging & Diagnostics

If the cause of a problem is unclear:

- add diagnostic logging FIRST
- do NOT apply speculative fixes

Logging must:
- print to console
- be structured and searchable
- include context keys

---

## 9. Code Style (MANDATORY)

- Use **tabs** for indentation
- Opening curly braces must be on a **new line**

---

## 10. MongoDB Rules (CRITICAL)

### A) DB Contract (Required)
- One contract per collection
- Path: `/db/contracts/<collection>.yml` (or .json)
- Must include:
  - collection name
  - indexes (keys, uniqueness, partials, collation)
  - purpose of each index
  - business invariants
  - all write entrypoints

Any query/write change must update the contract.

---

### B) Central Index Registry
- Single shared index module (e.g. `ensureIndexes`)
- No ad-hoc `createIndex` calls
- All services/scripts must use it

---

### C) Runtime Index Creation
- Distributed MongoDB-native lock
- Dedicated `_locks` collection
- Unique `{ name: 1 }` index
- Lease expiry + takeover
- Compare indexes by name + keys + options
- Never silently drop/recreate in prod

---

### D) Unique Index Safety
- Explicit rules for null / missing / empty
- Prefer partial unique indexes
- Preflight duplicate detection
- Fail fast unless explicitly allowed to auto-dedupe
- Treat E11000 as expected in races

---

### E) Idempotency
- Require idempotency keys for retryable ops
- Back with unique index
- Prefer atomic upserts

---

### F) Transactions
- Use sparingly
- Retry transient errors
- Keep scope minimal

---

### G) Query / Index Alignment
- Every query must have a matching index
- Or a documented justification

---

### H) Operational Safety
- Document index execution timing
- Log expected output
- Describe failure modes
- Include rollout considerations

---

## 11. `[codex-plan] — Mandatory Planning Mode`

When a task includes **`[codex-plan]`**, you are in **planning-only mode**.

### Forbidden in `[codex-plan]`
- writing code
- creating diffs
- modifying schemas or indexes
- creating tasks or scripts
- making assumptions

### Required behavior
- read repo docs and contracts
- identify real entrypoints
- ask clarifying questions using the **Mandatory Multiple-Choice Decision Format**
- surface risks and decision points

You may ask questions across **multiple messages**.

---

### Required `[codex-plan]` Response Format

1) **What I understand so far**  
2) **Open questions (blocking)** *(must be multiple-choice)*  
3) **Decision points (A/B/C)** *(must be multiple-choice)*  
4) **Risks & constraints**  
5) **What will happen after clarification**

---

### Exit Condition
You may exit `[codex-plan]` only when:
- all questions are answered
- no ambiguity remains

Then:
- create **one single executable task**
- list everything it will do
- wait for explicit approval

---

## 12. Task Checklist Completion Gate (MANDATORY)

When a user provides an explicitly numbered or bulleted task list intended for execution (not examples, options, or discussion lists), Codex MUST automatically convert that list into a checklist before execution.

If it is unclear whether a list is intended for execution, Codex MUST STOP and ask for clarification using the mandatory multiple-choice format before converting it into a checklist.

Checklist tracking protocol:
- Codex MUST keep checklist state visible in conversation and update status changes explicitly
- Codex MUST mark an item complete only after completing the work or after explicit user confirmation
- If an item is blocked or fails, Codex MUST report the failure, keep the item open, and await user direction

Checklist enforcement requirements:
- Codex MUST map every provided task to a checklist item
- Codex MUST NOT skip or silently drop any checklist item
- Codex MUST complete all non-PR checklist items before creating or opening any PR for that specific work
- Codex MAY include "create/open PR" as the final checklist item and mark it complete only when the PR is actually created/opened
- If any non-PR checklist item is incomplete, Codex MUST NOT create or open a PR unless the user explicitly approves splitting work into multiple PRs; in that case, Codex MUST clearly identify which checklist items are covered by the current PR and MUST keep deferred items open for future PRs

Scope boundaries:
- In `[codex-plan]` mode, checklist conversion is informational only until execution is explicitly approved
- In PR Review Mode (Section 13), this checklist gate applies only to new execution task lists in the current request, not to pre-existing review comments unless the user requests checklist execution for them
- If the user explicitly identifies a list as examples/options/discussion, Codex MUST NOT convert it into an execution checklist

---

## 13. PR Review Mode (INTENT PRESERVATION — CRITICAL)

When the user comments **`@codex change`** in a PR:

- review all comments, discussions, and review feedback
- apply only the changes that are **explicitly requested or clearly implied**
- resolve review comments accurately and minimally

### 13.1 Original Project Intent Preservation (NON-NEGOTIABLE)

When making changes in response to PR comments or reviews:

- You MUST NOT deviate from, reinterpret, or evolve the **original intent of the project**
- You MUST NOT introduce new goals, scope, abstractions, patterns, or behaviors unless explicitly approved
- You MUST treat the existing implementation as **intentional**, not accidental

#### This includes (but is not limited to):
- architecture choices
- data models
- control flow
- performance tradeoffs
- operational assumptions
- security posture
- backward compatibility guarantees

### 13.2 How to Handle Ambiguous PR Feedback

If a PR comment or review suggestion:
- could change system behavior
- could broaden or narrow scope
- could alter semantics or guarantees
- could impact downstream users
- could “clean up”, “simplify”, or “improve” behavior beyond the stated request

You MUST **STOP and ask clarifying questions** using the **mandatory multiple-choice format (A/B/C)** before making changes.

Example:

> “This review comment could be interpreted in multiple ways.  
> Please confirm which interpretation matches the original project intent.”

Then present options.

---

### 13.3 Forbidden in PR Review Mode

- “Improving” design beyond the comment
- Refactoring for elegance or style
- Reinterpreting intent based on best practices
- Applying reviewer suggestions that conflict with existing behavior
- Making changes “because it makes more sense”

If a suggestion conflicts with existing behavior:
- surface the conflict
- explain the impact
- ask for a decision
- **do not resolve it silently**

---

### 13.4 Acceptance Criteria

After applying PR changes, the result must satisfy **all** of the following:

- original project intent is preserved
- existing behavior remains unchanged unless explicitly approved
- backward compatibility is maintained
- no new assumptions are introduced
- changes are traceable directly to PR comments

If no changes are needed after review:
- explicitly reply: **“No changes are needed.”**

---

**PR feedback is not permission to reinterpret the project.  
Intent preservation overrides reviewer preference.**

---


## 14. Repository Hygiene Guardrail (Git Metadata + Python Bytecode)

To prevent CI/git reference corruption regressions:

- Never run tooling that writes into `.git/**` (including generated artifacts, caches, or bytecode).
- Ensure Python-based tooling jobs that operate on repository files set `PYTHONDONTWRITEBYTECODE=1`.
- Treat any generated `__pycache__/` or `*.pyc` under `.git/` as invalid state; remove/avoid it before Git operations.

## FINAL REMINDER

If uncertainty exists at any point:

**ASK QUESTIONS (MULTIPLE-CHOICE). DO NOT EXECUTE.**

Accuracy > speed.  
Safety > convenience.  
Backward compatibility is mandatory.
---
