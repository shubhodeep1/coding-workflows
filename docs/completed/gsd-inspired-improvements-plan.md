# GSD-Inspired Improvements (Unattended Pipeline + Scripts + Operator Surface)

## Archived status

This file is the canonical completed-plan record for tracking issue `#3303` / `#3351`. The closeout summary below reflects a re-audit of the shipped repository state on 2026-07-08 UTC; the historical plan text that follows is preserved for context, and where it conflicts with the closeout summary, the closeout summary is authoritative.

## Closeout summary

All ten items shipped and wired, re-audited against `origin/main` on 2026-07-08 UTC:

- Phase-prompt size budgets (`tests/prompt_size_budget.py`, CI-wired), reference blocks (`prompts/references/*`), plan self-check (`PLAN_SELF_CHECK`), required severity classification, memory-write injection guard (`scripts/memory_injection_patterns.py`), predicate-format facts in `agents.md`, inventory drift-control test (`tests/inventory_parity.py`), per-task commit discipline, operator install profiles (`workflow-templates/profiles/*`), and `docs/INVENTORY.md`.

Notes: the renderer shipped as `scripts/render_prompt.py` (the plan text names `render_prompt.sh`). One residual defense-in-depth gap — `scripts/memory_helpers.sh` has no injection-flag passthrough — is non-functional because all writes route through the Python guard in `ai_memory_lib.py`.

---

## Summary

Adopt ten additive improvements distilled from
[`gsd-build/get-shit-done`](https://github.com/gsd-build/get-shit-done)
(`v1.42.x`, npm-installable Claude Code meta-prompting / spec-driven dev
system, 67 commands / 33 agents / 13 hooks) into our **unattended pipeline
prompts**, **workflow orchestration scripts**, and **operator / consumer-repo
experience**. Each item names a gsd-build source file or mechanism, an
explicit target in this repo, and the proposed wording or wiring. No
interactive `.claude/commands` are added in this plan — that surface is
deliberately out of scope per the clarification round and goes in the
companion `docs/gsd-future-improvements.md`.

## Context

The trigger is the user's question "go through
https://github.com/gsd-build/get-shit-done and tell me what learnings and
improvements we can get from it to apply to our repo." A research pass
through the gsd-build root, `docs/ARCHITECTURE.md`, `docs/INVENTORY.md`,
`CONTEXT.md`, `CLAUDE.md`, every `agents/gsd-*.md` agent name, every
`hooks/gsd-*.{js,sh}` hook, and the README produced four buckets:

1. **Directly portable mechanisms** — prompt-size budgets, reference-block
   extraction, severity classification, pre-execution plan checks,
   injection guards, machine-greppable predicate format, inventory
   drift-tests, atomic per-task commits, install profiles, INVENTORY roster.
   Adopted in this plan.
2. **Interactive-Claude-Code-only items** — slash-command surface, context
   monitor hooks, statusline metrics, prompt-injection PreToolUse hook.
   Surfaced in the companion doc as **S1–S4 deferred (interactive scope)**.
3. **Contentious mechanism conflicts** — `absent=enabled` flag default,
   adversarial FORCE-stance language, forced per-prompt line shrink,
   predicate-only docs, npm bootstrap installer. Surfaced in the companion
   doc as **C1–C5**.
4. **Out of scope, will not adopt** — gsd's solo-developer milestone
   framework (`/gsd-complete-milestone` / `/gsd-new-milestone`), gsd's
   `--dangerously-skip-permissions` install ergonomics, gsd's
   roadmap/milestone state graph. We are an org-scale unattended pipeline,
   not a solo loop.

The closest precedents in this repo are
[`docs/plans/apply-ai-tools-learnings-plan.md`](./apply-ai-tools-learnings-plan.md)
and
[`docs/plans/symphony-inspired-improvements-plan.md`](symphony-inspired-improvements-plan.md).
This plan reuses their template — each adopted item names a source file, a
target file in this repo, the wording or wiring, and any §6/§10/§14/§15
constraints it triggers.

CLAUDE.md sections that bind this work:

- **§5 Minimal Change Set** — "Extend existing mechanisms — never compete
  with them." Items 1–6, 8, 10 extend existing files (`prompts/`,
  `scripts/`, `tests/`, `agents.md`, `README.md`); item 7 adds a single new
  test file; item 9 adds a single new `workflow-templates/` profile manifest.
- **§6 Backward Compatibility / Naming Immutability** — No identifier is
  renamed. Item 2 introduces new `prompts/references/*.txt` shared blocks
  but keeps every existing `prompts/mode-*.txt` filename intact and
  references the new blocks via `{{...}}` placeholders rather than
  filesystem `@-references` (we'd need a renderer change for `@-refs`; out
  of scope).
- **§14 Consumer Repo Registry** — Item 9 (install profiles) changes the
  shape of `workflow-templates/` consumption. Every repo in
  `.github/ai/consumer_repos.json` (11 repos) keeps working under the
  "full" profile by default; profile selection is opt-in via a new
  `vars.WORKFLOW_PROFILE` repo-var.
- **§15 GitHub API Call Hygiene** — None of the adopted items add new
  `gh api` callsites. Item 5 (injection guard) operates entirely on local
  text; item 7 (drift test) reads only the filesystem.

## Goals

Each goal is falsifiable by re-reading the named file after the corresponding
item lands.

1. **Phase-prompt size budgets** — every `prompts/mode-*.txt` carries a
   declared tier and a CI test (`tests/prompt_size_budget.py` or
   equivalent) fails the build when the file exceeds its tier. Source:
   gsd-build `tests/workflow-size-budget.test.cjs`,
   `docs/ARCHITECTURE.md#progressive-disclosure-for-workflows`.
2. **Reference blocks** — at least three reusable prompt blocks
   (`verification-loop.txt`, `output-contract.txt`, `severity-classification.txt`)
   live in `prompts/references/` and are referenced from at least two
   phase prompts each. Source: gsd-build `get-shit-done/references/*.md`,
   `gsd-planner.md`'s `@~/.claude/get-shit-done/references/mandatory-initial-read.md`.
3. **Pre-execution plan check** — `prompts/mode-plan.txt` ends with a
   self-check sub-pass that requires BLOCKER/WARNING classification of any
   gaps before `STATUS: CLEAR` can be emitted. Source: gsd-build
   `agents/gsd-plan-checker.md` adversarial FORCE-stance.
4. **Severity classification required** — reviewer + judge outputs MUST
   carry `SEVERITY: BLOCKER|MAJOR|NIT`; outputs missing severity are
   invalid. Source: gsd-build `gsd-plan-checker.md` Required Finding
   Classification block.
5. **Memory-write injection guard** — every candidate record committed to
   the `ai-memory` branch via `scripts/ai_memory.py` or
   `scripts/memory_helpers.sh` is scanned for the 14 gsd-build injection
   patterns; matches log a `MEMORY_INJECTION_DETECTED` warning to stderr
   and tag the record with `injection_suspected: true` but do not block
   the write (advisory-only, mirroring gsd-build's policy). Source:
   gsd-build `hooks/gsd-prompt-guard.js` regex set.
6. **Predicate-format facts in `agents.md`** — the "Stable log prefixes"
   and "Repo-specific batching helpers" sections gain
   machine-greppable `PREDICATE.subkey=value` lines alongside existing
   prose. Source: gsd-build `CONTEXT.md` header rule "Each operational
   fact is a single-line predicate (`CLASS.subkey=value`)."
7. **Inventory drift-control test** — a new `tests/inventory_parity.py`
   verifies every `prompts/mode-*.txt`, every `.github/workflows/*.yml`,
   and every `scripts/*` file appears in the README and `agents.md`
   roster (or is on a documented exemption list). Source: gsd-build
   `tests/inventory-counts.test.cjs`, `tests/commands-doc-parity.test.cjs`.
8. **Per-task atomic commit discipline** — `prompts/mode-implement.txt`
   gains an explicit "one logical change = one commit" rule, with a
   commit-message format that names the plan task it satisfies. Source:
   gsd-build `agents/gsd-executor.md` atomic-commit pattern (35 KB agent).
9. **Operator install profiles** — `workflow-templates/profiles/{core,standard,full}.txt`
   list which wrappers ship under each profile; `update_workflows.yml`
   reads `vars.WORKFLOW_PROFILE` and installs only the listed wrappers.
   Default profile = `full` (no behaviour change for existing consumer
   repos). Source: gsd-build README `--profile=core,standard,full`,
   `/gsd:surface` runtime toggle.
10. **Authoritative INVENTORY.md** — `docs/INVENTORY.md` enumerates every
    phase prompt, every workflow, every script with a one-line role, and
    is anchored by the drift-control test from goal 7. Source: gsd-build
    `docs/INVENTORY.md` v1.36.0 pin.

## Non-goals

- **No interactive `.claude/commands` additions.** The user excluded the
  interactive Claude Code surface from this plan. Deferred items go in
  `docs/gsd-future-improvements.md`.
- **No new agents / subagents.** gsd-build has 33 agents; we are not
  adopting any. Our codex-cli model invocations are phase-shaped, not
  agent-shaped, and the existing precedent
  (`docs/plans/apply-ai-tools-learnings-plan.md` §7) bounds the
  subagent-related rules we already adopted.
- **No `@-reference` filesystem syntax.** Reference blocks are wired via
  `{{...}}` placeholders resolved by the existing `scripts/render_prompt.sh`
  expansion path. Filesystem-native `@-references` would require a renderer
  refactor; goes in the companion doc as **S5**.
- **No npm / installer bootstrap.** We have a workflow-dispatch–driven
  propagation model (`update_workflows.yml` against
  `.github/ai/consumer_repos.json`). Replacing it with an npm CLI is a
  separate project; goes in the companion doc as **C5**.
- **No milestone / roadmap state graph.** gsd-build is built around a
  solo developer's project lifecycle; we are issue-driven and orchestrator-
  managed. Not adopting.
- **No `--dangerously-skip-permissions` ergonomics or runtime hot-reload.**
  Out of scope for unattended pipelines.
- **No mass rename of stable log prefixes** listed in `agents.md` —
  forbidden by §6.

## Constraints

- **§5** — every item below extends, not replaces. Reviewer / judge
  prompts that already carry partial severity language (`prompts/review-consolidator.txt`
  has `SEVERITY: blocker | high | med | low`) are tightened, not rewritten.
- **§6** — no identifier rename. Item 4 aligns reviewer-side severity
  vocabulary with consolidator-side vocabulary via additive aliases
  (`high == MAJOR`, `med | low == NIT` translation table) rather than
  forcing a one-shot rename.
- **§10** — none of the adopted items touch a MongoDB collection or index.
  No `/db/contracts/*` updates required.
- **§14** — item 9 is propagation-relevant. The default `WORKFLOW_PROFILE`
  is `full`, so no consumer repo sees a behaviour change unless its
  operator explicitly opts down to `core` or `standard`. Every repo in
  `.github/ai/consumer_repos.json` keeps its current wrapper set.
- **§15** — items 1, 2, 3, 4, 5, 6, 8, 10 are prose-only (no API calls).
  Item 7 (drift test) and item 9 (profile manifest) read only local files
  in CI. No new `gh api` calls anywhere.
- **Security** — item 5 (memory-write injection guard) is the only
  security-relevant change. It is advisory-only by design (matches
  gsd-build's policy) — blocking false-positives would break the
  fail-open invariant that every `ai-memory` write must satisfy
  (`README.md` §"Memory System": "a memory error never fails the
  workflow").
- **Performance** — items 1, 7 add CI checks that run on push; combined
  expected runtime <5 s on the repo's current size. Item 5 adds a
  ~14-regex scan per candidate-record write; per-record cost <1 ms.

## Approach

Each item is a **prose-only or single-file additive change** to a named
existing file or a new single file in a documented location. Items are
ordered roughly by blast radius (smallest first) so a partial roll-out
still produces a working pipeline; items 1–8 ship as one PR, items 9–10
as a separate follow-up PR if reviewer load is a concern.

Three design decisions worth surfacing:

- **`{{...}}` placeholders, not `@-references`.** gsd-build's
  `@~/.claude/get-shit-done/references/foo.md` syntax requires a Claude
  Code-side resolver. Our `render_prompt.sh` already supports
  `{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}`–style placeholders; extending
  that mechanism to `{{REFERENCE_VERIFICATION_LOOP}}` is a 10-line
  script change. Going to filesystem `@-refs` would mean teaching every
  codex-cli phase about a new include syntax — deferred to **S5**.
- **Advisory-only injection guard.** gsd-build's
  `hooks/gsd-prompt-guard.js` is also advisory ("Why advisory-only:
  Blocking would prevent legitimate workflow operations"). Adopting the
  same posture preserves our existing fail-open memory contract; the
  alternative (blocking) would create a new failure mode where a
  legitimately written rule like "ignore previous instructions for this
  field" would deadlock memory writes.
- **Default profile = `full`.** gsd-build's installer is profile-default
  (user picks `core` interactively). We default to `full` so existing
  consumer repos see no change. Profile selection is opt-in via
  `vars.WORKFLOW_PROFILE`. This matches §5's "extend existing
  mechanisms" principle: `update_workflows.yml` still installs every
  wrapper by default; profiles are a *narrowing* override, not a
  replacement.

Alternatives considered and rejected:

- **Wholesale slash-command adoption.** gsd-build is fundamentally
  designed around 67 interactive slash commands. Mirroring even a subset
  (e.g. `/discuss-phase`, `/verify-work`) in `.claude/commands/` would
  add a parallel surface that competes with our existing unattended
  pipeline (clarify, plan, implement, review, validate). Per the
  clarification round, this is deferred to **S1** in the companion doc.
- **Replacing prose `agents.md` with predicate-only format.** Conflicts
  with §6 — `agents.md` is consumed by both our orchestrator-poller
  loop and downstream consumer-repo prompts that grep for specific
  prose strings. Going predicate-only would break those greps. Item 6
  adds predicates *alongside* existing prose instead.

## Implementation Steps

Each step lists files, change in one sentence, and any preconditions.

### Item 1 — Phase-prompt size budgets

1. **New `tests/prompt_size_budget.py`** — read every `prompts/mode-*.txt`
   and `prompts/review-*.txt`, derive each file's tier from a new
   frontmatter line `# tier: DEFAULT|LARGE|XL` (default `DEFAULT` if
   missing), assert `wc -l <= {250 | 500 | 800}`, exit nonzero on
   violation. Hook into `.github/workflows/ci.yml`.
2. **Add tier frontmatter to every `prompts/mode-*.txt` and `prompts/review-*.txt`** —
   one comment line per file. Current sizes (from `wc -l`):
   - DEFAULT (≤250): every prompt except the four below.
   - LARGE (≤500): `mode-validate-diagnose.txt` (198 — comfortably under
     but pre-flagged as LARGE for headroom), `mode-validate-fix-harness.txt`
     (276 — exceeds DEFAULT, marks LARGE).
   - XL (≤800): `mode-validate-generate.txt` (809 — exceeds XL by 9 lines;
     extract the ≥10-line `### Validation harness output contract` block
     into `prompts/references/validate-output-contract.txt` in step 2 of
     item 2, bringing the parent under 800).
3. **Preconditions:** item 2 step 1 lands first so the extracted reference
   block exists.

### Item 2 — Reference blocks

1. **New `prompts/references/verification-loop.txt`** (~30 lines) — the
   typecheck → lint → tests → build → smoke gate-order rule already
   adopted in `apply-ai-tools-learnings-plan.md` goal 5; extracted into
   a single reusable block. Referenced from `prompts/mode-implement.txt`,
   `prompts/mode-validate-generate.txt`, `prompts/mode-validate-fix-harness.txt`.
2. **New `prompts/references/output-contract.txt`** (~20 lines) — the
   `<preamble>` / `<checkpoint>` / `<final_summary>` status-update
   cadence rules from `apply-ai-tools-learnings-plan.md` goal 4;
   extracted. Referenced from every `prompts/mode-*.txt`.
3. **New `prompts/references/severity-classification.txt`** (~25 lines) —
   the BLOCKER / MAJOR / NIT vocabulary + the "issues without severity
   are invalid output" rule. Referenced from
   `prompts/review-reviewer-checklist.txt`,
   `prompts/review-consolidator.txt`,
   `prompts/mode-judge-review-blocked.txt`, `prompts/mode-judge.txt`.
4. **Extend `scripts/render_prompt.sh`** — add three new placeholder
   substitutions: `{{REFERENCE_VERIFICATION_LOOP}}`,
   `{{REFERENCE_OUTPUT_CONTRACT}}`, `{{REFERENCE_SEVERITY_CLASSIFICATION}}`.
   Mechanism mirrors existing `{{SERENA_EFFICIENCY_BLOCK_READ_ONLY}}`
   handling (single sed/awk substitution from a known file). Renderer
   fails if a placeholder is unresolved (matches existing contract).
5. **Edit phase prompts** to reference the blocks via the placeholders.
   No existing rule text is deleted; the placeholder is inserted at the
   appropriate section and the existing prose is replaced *only if* the
   reference block fully captures the rule (otherwise the placeholder
   sits *alongside* the prose). Preserves §5 minimum-change discipline.

### Item 3 — Pre-execution plan check

1. **Edit `prompts/mode-plan.txt`** — append a new terminal section
   `## Pre-execution self-check` (≤40 lines) that requires the model to
   emit either `STATUS: CLEAR` plus a single line `PLAN_SELF_CHECK: PASS`
   or a list of `PLAN_SELF_CHECK: BLOCKER: <one-line>` /
   `PLAN_SELF_CHECK: WARNING: <one-line>` findings before exiting. If
   any `BLOCKER` is emitted, the plan is `STATUS: NOT_CLEAR` and goes
   back to clarification. This is a single-prompt analogue of gsd's
   external `gsd-plan-checker` agent — we don't need a separate model
   call because the plan model is already at `xhigh` reasoning and the
   self-check is ~50 extra tokens.
2. **Edit `scripts/plan_process.sh`** (or the plan-workflow consumer)
   to parse `PLAN_SELF_CHECK: BLOCKER:` lines and propagate them as a
   plan-reopened signal — fail the plan workflow with a structured
   error if any BLOCKER is present. **Preconditions:** verify the
   plan-workflow consumer name (`scripts/render_prompt.sh` and
   `.github/workflows/plan.yml` show `prompts/mode-plan.txt` is read by
   `plan.yml`; the parsing change goes in the workflow step that
   currently consumes the plan output).

### Item 4 — Severity classification required

1. **Edit `prompts/review-reviewer-checklist.txt`** — add a "Severity is
   mandatory" subsection citing `prompts/references/severity-classification.txt`
   via `{{REFERENCE_SEVERITY_CLASSIFICATION}}` (after item 2). Replaces
   the existing implicit-severity convention with explicit.
2. **Edit `prompts/review-consolidator.txt`** — clarify the existing
   `SEVERITY: blocker | high | med | low` line to: "Reviewer-side
   `BLOCKER` maps to `blocker`; `MAJOR` maps to `high`; `NIT` maps to
   `med`. `low` is reserved for advisory-only consolidator-internal
   downgrades and is never emitted by reviewers." This is an alias
   convention; nothing renames.
3. **Edit `prompts/mode-judge.txt` and `prompts/mode-judge-review-blocked.txt`** —
   require judges to surface the same vocabulary when summarising
   reviewer output. Reuse `{{REFERENCE_SEVERITY_CLASSIFICATION}}`.
4. **Add to `prompts/references/severity-classification.txt`** the rule:
   "Issues without an explicit severity classification are not valid
   output and MUST be re-emitted with severity." (Source: gsd-build
   `gsd-plan-checker.md` `<adversarial_stance>`.)

### Item 5 — Memory-write injection guard

1. **New `scripts/memory_injection_patterns.py`** — a single-module file
   exposing `INJECTION_PATTERNS` (list of 14 regexes from gsd-build
   `hooks/gsd-prompt-guard.js` lines 17–32) and a function
   `scan(text) -> list[str]` returning matched pattern names.
2. **Edit `scripts/ai_memory.py`** — at every `write_candidate`–shaped
   entrypoint (search `def write_` and `def _write_` in
   `scripts/ai_memory_lib.py` and `scripts/ai_memory.py`; the exact
   function names need to be verified during implementation, not
   guessed here), call `scan(record_body)` before commit; on any match,
   emit `AI_MEMORY_TELEMETRY: {op: 'injection_scan', ok: true,
   matches: [...]}` to stderr (matches existing telemetry conventions
   per README §"Telemetry") and add a top-level field
   `injection_suspected: true` to the record JSON. Never block.
3. **Edit `scripts/memory_helpers.sh`** — pass the new flag through any
   shell-side write wrappers so the telemetry surface is consistent.
4. **No change to `AI_MEMORY_ENABLED` kill-switch behaviour.** The guard
   is purely additive.

### Item 6 — Predicate-format facts in `agents.md`

1. **Edit `agents.md`** — under "Stable log prefixes (contractual)", add
   a single new subsection `### Predicate roster (machine-greppable)`
   with one line per stable prefix in the form
   `LOG_PREFIX.name=<prefix>` (e.g. `LOG_PREFIX.name=LABEL_REPAIR`).
   Add a similar subsection under "Repo-specific batching helpers":
   `BATCH_HELPER.name=_fetch_candidate_issue_details_graphql`,
   `BATCH_HELPER.module=scripts/orchestrate_poll_process.sh`. The
   existing prose is preserved verbatim above; predicates are
   *alongside*, not *instead of*.
2. **Edit `unattended_system_instructions.md`** — extend the existing
   "Repo-specific batching helpers" callout there to mirror the new
   predicate format (if the consumer pipeline ever wants to grep for
   these without parsing prose).

### Item 7 — Inventory drift-control test

1. **New `docs/INVENTORY.md`** — one section per surface (phase prompts,
   workflows, scripts, prompt references, audit-gate assets) with a
   one-line role per file. Source the role from existing per-file
   header comments or README mentions (no new prose where the README
   already documents the file).
2. **New `tests/inventory_parity.py`** — for each surface, glob the
   filesystem, glob the README plus `agents.md` plus
   `docs/INVENTORY.md`, assert one-to-one correspondence (or named
   exemption). Pull exemption list from a new
   `tests/inventory_exemptions.txt` (initially empty; entries land
   with a one-line justification).
3. **Hook into `.github/workflows/ci.yml`** — run `tests/inventory_parity.py`
   on push/PR. Failure mode: print the drifted file(s); operator must
   either document the new file or add it to the exemption list.

### Item 8 — Per-task atomic commit discipline

1. **Edit `prompts/mode-implement.txt`** — append a `## Commit
   discipline` section (≤20 lines):
   - One logical change = one commit.
   - Commit message format: `<verb>: <change> (plan task <N.M>)`
     where `<N.M>` is the task ID from `PLAN.md` if present, else
     omit the parenthetical.
   - Bulk style edits or auto-format passes go in a separate trailing
     commit titled `chore: auto-format pass`.
   - Failed test fix-ups roll into the commit that introduced the
     failure (use `--amend` only on commits from the current `implement`
     run that have not yet been pushed).
2. **Verify alignment with existing review_autofix behaviour** — the
   `[ai-autofix]` commit-prefix convention in `review_autofix.yml`
   continues to apply; the atomic-commit rule is a *plan-execution*
   discipline, not an autofix discipline.

### Item 9 — Operator install profiles

1. **New `workflow-templates/profiles/core.txt`** — one-wrapper-per-line
   list:
   ```
   ai-clarify.yml
   ai-plan.yml
   ai-implement.yml
   ai-review.yml
   ai-issue-pr-status.yml
   ai-cancel-on-pr-close.yml
   ```
2. **New `workflow-templates/profiles/standard.txt`** — core + orchestrator
   + validation:
   ```
   <every line in core.txt>
   ai-orchestrate.yml
   ai-orchestrate-poll.yml
   ai-orchestrate-clarify-respond.yml
   ai-validate.yml
   review_rb_judge_dispatch.yml
   ```
3. **New `workflow-templates/profiles/full.txt`** — every wrapper in
   `workflow-templates/`. Default.
4. **Edit `.github/workflows/update_workflows.yml`** — read
   `vars.WORKFLOW_PROFILE` (default `full`), read the matching
   `workflow-templates/profiles/<profile>.txt`, install only the listed
   wrappers. Wrappers already present in the consumer repo that are
   *not* listed in the profile are LEFT IN PLACE (no removals) —
   profile downgrade does not delete files, only stops creating new
   ones. This preserves §6 / §14 contracts.
5. **Edit `README.md`** — add a "Install profiles" subsection under
   "Quickstart" documenting `vars.WORKFLOW_PROFILE` and the three
   manifest files.
6. **Document in `agents.md`** — add a one-line predicate
   `PROFILE.default=full` and one line per defined profile.

### Item 10 — Authoritative INVENTORY.md

See item 7. The `docs/INVENTORY.md` file added there is the authoritative
roster; this item is the *act of writing the prose* (the test only
enforces drift-control parity). One-line role per file, sourced from
existing per-file headers where they exist.

## Files & Modules

- `[new]` `docs/plans/gsd-inspired-improvements-plan.md` — this file.
- `[new]` `docs/gsd-future-improvements.md` — companion deferred /
  contentious items (separate ship-alongside doc).
- `[new]` `docs/INVENTORY.md` — authoritative roster.
- `[new]` `prompts/references/verification-loop.txt`.
- `[new]` `prompts/references/output-contract.txt`.
- `[new]` `prompts/references/severity-classification.txt`.
- `[new]` `prompts/references/validate-output-contract.txt` — extracted
  to bring `mode-validate-generate.txt` under XL tier.
- `[new]` `tests/prompt_size_budget.py`.
- `[new]` `tests/inventory_parity.py`.
- `[new]` `tests/inventory_exemptions.txt`.
- `[new]` `scripts/memory_injection_patterns.py`.
- `[new]` `workflow-templates/profiles/core.txt`.
- `[new]` `workflow-templates/profiles/standard.txt`.
- `[new]` `workflow-templates/profiles/full.txt`.
- `[edit]` `prompts/mode-plan.txt` — append pre-execution self-check.
- `[edit]` `prompts/mode-implement.txt` — append commit discipline +
  reference placeholders.
- `[edit]` `prompts/mode-validate-generate.txt` — extract output contract;
  insert reference placeholder.
- `[edit]` `prompts/mode-validate-fix-harness.txt` — insert reference
  placeholders.
- `[edit]` `prompts/mode-validate-diagnose.txt` — insert reference
  placeholders.
- `[edit]` `prompts/review-reviewer-checklist.txt` — severity-required
  placeholder + alias rule.
- `[edit]` `prompts/review-consolidator.txt` — severity vocabulary
  alignment.
- `[edit]` `prompts/mode-judge.txt` and `prompts/mode-judge-review-blocked.txt` —
  severity placeholder.
- `[edit]` `scripts/render_prompt.sh` — add three placeholder
  substitutions.
- `[edit]` `scripts/ai_memory.py`, `scripts/ai_memory_lib.py`,
  `scripts/memory_helpers.sh` — wire injection guard.
- `[edit]` `agents.md` — predicate roster subsections.
- `[edit]` `unattended_system_instructions.md` — predicate-format batch
  helpers callout.
- `[edit]` `.github/workflows/update_workflows.yml` — read
  `vars.WORKFLOW_PROFILE`.
- `[edit]` `.github/workflows/ci.yml` — add prompt-size + inventory-parity
  test steps.
- `[edit]` `README.md` — Install profiles subsection.

## Data Model / Index Changes

None. No MongoDB collection touched. `/db/contracts/*` updates not
required.

## Tests

- **Unit** — `tests/prompt_size_budget.py` and `tests/inventory_parity.py`
  are themselves the new test coverage; both run in CI on push/PR.
- **Integration** — re-run the existing smoke test for clarify / plan /
  implement / review on a single consumer repo (e.g.
  `shubhodeep1/digital_pa`) to verify the new `{{REFERENCE_*}}`
  placeholders resolve correctly via `render_prompt.sh`. Acceptance:
  every smoke phase emits its expected `STATUS: …` line.
- **Manual** — operator-side, set `vars.WORKFLOW_PROFILE=core` on one
  test consumer repo, run `update_workflows.yml`, verify only the
  six core wrappers are touched and no other wrappers are removed.
  Acceptance: `git diff --stat` on the consumer repo shows only the
  intended wrapper edits.
- **Smoke** — the `nightly-validation-selftest.yml` workflow re-runs
  the validate phase end-to-end; verify the size-budget test passes
  in that nightly bake.

## Risks & Mitigations

- **Risk:** Item 2's `{{REFERENCE_*}}` placeholder mechanism breaks
  silently if `render_prompt.sh` is invoked from a code path that does
  not source the reference files.
  - **Mitigation:** The existing renderer contract already fails on
    unresolved placeholders. Item 2 step 4 explicitly preserves that
    behaviour; CI catches any regression.
- **Risk:** Item 3 (`PLAN_SELF_CHECK`) over-rejects benign plans
  because the plan model is asked to find blockers in its own output.
  - **Mitigation:** Treat the self-check as a *signal*, not a *gate* —
    the plan workflow can continue to `ai:awaiting-approval` even on a
    WARNING-only self-check. Only BLOCKER fails the plan. Operators
    can disable the self-check via a new `vars.PLAN_SELF_CHECK_ENABLED`
    repo-var (default `true`).
- **Risk:** Item 4 (severity required) breaks existing reviewer outputs
  that omit severity.
  - **Mitigation:** Add a parser fallback in `scripts/review_parse.sh`
    (or equivalent) that defaults missing severity to `MAJOR` and
    emits a `REVIEWER_SEVERITY_DEFAULT` warning; this is the same
    fail-open posture as `REVIEW_PARSER_FAILOPEN` (default `1`).
- **Risk:** Item 5 (injection guard) false-positives on legitimate
  text that contains a flagged phrase (e.g. a plan note that quotes
  "ignore previous instructions" as the example of an injection).
  - **Mitigation:** Advisory-only by design — the record is still
    written; only the `injection_suspected: true` flag is set.
    Downstream reads can choose to filter or not.
- **Risk:** Item 9 (profile manifest) misaligned with `update_workflows.yml`
  results in a consumer repo missing a wrapper after an upstream change.
  - **Mitigation:** Default profile is `full` so existing consumer
    repos see no behaviour change. Profile downgrade explicitly does
    NOT remove already-installed wrappers (item 9 step 4); operators
    must remove manually.
- **Risk:** Item 7 (inventory drift-control test) creates churn on
  every new prompt / script addition.
  - **Mitigation:** The exemption file (`tests/inventory_exemptions.txt`)
    is the escape hatch — adding a one-line entry with a justification
    is the lightest possible workflow when documentation is genuinely
    deferred.
- **Risk:** Per-task atomic commits (item 8) conflict with codex-cli's
  current commit-batching behaviour.
  - **Mitigation:** The rule is a prompt-level guideline, not a
    process enforced by a hook. codex-cli will still batch where it
    cannot decompose; we accept that and verify on smoke runs that
    median commits-per-implement-run goes from 1 to 2–4 (target).

## Rollout

- **Phase A (PR 1, this PR):** Ship `docs/plans/gsd-inspired-improvements-plan.md`
  (this file) and `docs/gsd-future-improvements.md` (companion).
- **Phase B (PR 2):** Items 1, 2, 6, 7, 10 — prose-only / file-additive
  changes with zero behavioural impact on existing runs. Ships behind
  no flag; CI gates the size-budget and inventory-parity tests.
- **Phase C (PR 3):** Items 3, 4, 8 — prompt edits affecting plan,
  reviewer, judge, implement. Gate item 3 behind
  `vars.PLAN_SELF_CHECK_ENABLED` (default `true`); items 4 and 8 are
  prose changes with fail-open parsers.
- **Phase D (PR 4):** Item 5 — memory injection guard, advisory-only.
  Gate behind `vars.MEMORY_INJECTION_SCAN_ENABLED` (default `true`).
  Telemetry surface verified in the next workflow-log-analysis pass.
- **Phase E (PR 5):** Item 9 — install profiles. Default `full`, opt-in
  for `core` / `standard` via a single repo-var. Document in README.
  Consumer-repo propagation per `.github/ai/consumer_repos.json` (§14)
  is automatic — no per-repo migration needed.
- **Rollback path:** every phase is reverted independently by removing
  the new file(s) and reverting the placeholder insertions or the
  `vars.*` repo-var. Phase D rollback additionally requires flipping
  `MEMORY_INJECTION_SCAN_ENABLED` to `false`. No rollback requires
  rewriting history or amending merged commits.
- **Consumer-repo propagation (§14):** when Phase E lands, the next
  `update_workflows.yml` dispatch tagged `@stable` reaches all 11
  repos in `.github/ai/consumer_repos.json`. Default-`full` means no
  behaviour change unless an operator opts down.

## Open Questions

- **OQ1:** Should the `PLAN_SELF_CHECK` (item 3) actually be a *second
  LLM call* (a dedicated plan-checker phase, mirroring gsd-build's
  separate `gsd-plan-checker` agent), or remain a self-emitted block
  from the plan model? The current proposal is the single-prompt
  variant for cost reasons. If we observe the plan model rarely
  self-flags real BLOCKERs in smoke runs, a separate phase becomes
  the right design. Defer to a later iteration based on smoke evidence.
- **OQ2:** Should the injection-guard pattern set (item 5) include
  domain-specific patterns beyond gsd-build's 14? Candidates: codex-cli
  tool-name leaks (`apply_patch`, `multi_tool_use.parallel`) from
  `apply-ai-tools-learnings-plan.md` goal 1, GitHub-flavoured
  injection (`@stable`, `@codex change` redirect attempts). The
  initial PR ships gsd-build's 14 verbatim; domain patterns can layer
  in via a follow-up.
- **OQ3:** Should item 9's default profile flip to `standard` (drop
  the `workflow-log-analysis.yml` and `memory_maintenance.yml`
  wrappers from the default install) once the profile mechanism is
  proven? Not for this PR — default is `full`.

## References

- gsd-build/get-shit-done: <https://github.com/gsd-build/get-shit-done>
  (default branch `main`, sampled at this plan's write time).
- gsd-build `docs/ARCHITECTURE.md` — workflow-tier budgets,
  reference-block pattern, two-stage routing.
- gsd-build `docs/INVENTORY.md` — drift-control test surface.
- gsd-build `agents/gsd-planner.md`, `agents/gsd-plan-checker.md` —
  adversarial FORCE-stance, severity-required rule.
- gsd-build `hooks/gsd-prompt-guard.js` — 14-pattern injection regex set.
- gsd-build `hooks/gsd-context-monitor.js`,
  `hooks/gsd-phase-boundary.sh` — runtime hooks (deferred to companion
  doc as **S4**).
- gsd-build README `--profile=core|standard|full` —
  `npx get-shit-done-cc@latest`.
- This repo: `docs/plans/apply-ai-tools-learnings-plan.md`,
  `docs/ai-tools-future-improvements.md`,
  `docs/plans/symphony-inspired-improvements-plan.md` — template precedents.
- This repo: `CLAUDE.md` §§ 5, 6, 10, 14, 15 — binding constraints.
- This repo: `README.md` §"Memory System", §"Telemetry" — memory
  contract that item 5 must preserve.
