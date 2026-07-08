# Apply Learnings from `vercel-labs/open-agents`

## Archived status

This file is the canonical completed-plan record for tracking issue `#3376`. The closeout summary below reflects a re-audit of the shipped repository state on 2026-07-08 UTC; the historical plan text that follows is preserved for context, and where it conflicts with the closeout summary, the closeout summary is authoritative.

## Closeout summary

All nine items shipped and wired, re-audited against `origin/main` on 2026-07-08 UTC:

- Explore-first gates in plan/implement prompts, model-family reviewer overlays (`prompts/overlays/*` + renderer plumbing), the six-step plan readiness gate, verification-loop discipline, read-before-edit / no-surprise-edits guardrails, project-script + package-manager autodetect in validation prompts, advisory bash-safety heuristics, a categorised operational-lessons section in `agents.md`, and an opt-in tier-based prompt compaction helper (`scripts/openrouter_prompt_cache.py` + `tests/test_prompt_compaction.py`).

Items 2 and 9 remain opt-in/dormant by default, so unset behaviour is byte-identical to baseline.

---

## Summary

Adopt nine prompt- and script-level mechanisms distilled from
[`vercel-labs/open-agents`](https://github.com/vercel-labs/open-agents)
(the open-source Vercel reference app for background coding agents — 5.5k
stars, TypeScript, `packages/agent` + `.agents/skills/` + `docs/agents/`
structure) into this repo's **unattended prompt set** (`prompts/mode-*.txt`),
**reviewer fan-out scripts** (`scripts/render_prompt.sh`,
`scripts/review_run_reviewers.sh`), **agents.md operational facts**, and
**CLAUDE.md interactive-session policy**. The plan is strictly additive: no
identifier is renamed (§6), no MongoDB / contract surface is touched (§10),
and no consumer-repo propagation is triggered (§14).

## Context

The trigger is the user's question "go through
<https://github.com/vercel-labs/open-agents> and tell me what improvements
and learnings we can incorporate into our repo/project." A research pass on
2026-05-17 surveyed the upstream repo's root README, `docs/agents/*`
(`architecture.md`, `code-style.md`, `lessons-learned.md`,
`react-best-practices-audit.md`), `packages/agent/`
(`system-prompt.ts`, `open-agent.ts`, `subagents/` registry with
`explorer.ts` + `executor.ts` + `design.ts`, `tools/` set —
`ask-user-question.ts`, `bash.ts`, `fetch.ts`, `glob.ts`, `grep.ts`,
`read.ts`, `write.ts`, `task.ts`, `skill.ts`, `todo.ts`, plus
`path-security.ts`, `context-management/aggressive-compaction-helpers.ts`,
`context-management/cache-control.ts`), `packages/agent/docs/approval-system.md`,
the `.agents/skills/` set (13 skills including `code-review/SKILL.md` and
`plan-mode/SKILL.md`), and the `.github/workflows/ci.yml` definition.

The user (Q1–Q4 clarification batch, 2026-05-17) selected:

- **Q1 → A** — slug `open-agents-learnings`.
- **Q2 → B** — single-list comprehensive: one numbered list of every
  applicable item with source-file → target-file → wording, no companion
  doc, no three-bucket split.
- **Q3 → A+B** — in-scope target surfaces are `prompts/` + `agents.md` +
  `CLAUDE.md` (prompt-level adoption) and `scripts/` +
  `workflow-templates/` (behaviour adoption). `docs/` restructure and
  `.claude/` interactive surface (skills/hooks/commands) are out of scope
  for this plan.
- **Q4 → B** — out-of-scope items (Vercel sandbox / snapshots, Better Auth,
  React performance, Next.js patterns, GitHub App OAuth flows, monorepo
  packages split) are dropped silently rather than enumerated.

Closest precedents in this repo:

- [`docs/plans/awesome-claude-code-learnings-plan.md`](./awesome-claude-code-learnings-plan.md)
  — same "borrow mechanisms, drop conflicting parts" template.
- [`docs/plans/gsd-inspired-improvements-plan.md`](./gsd-inspired-improvements-plan.md)
  — same source-file → target-file enumeration shape.
- [`docs/plans/apply-ai-tools-learnings-plan.md`](./apply-ai-tools-learnings-plan.md)
  — same phase-per-theme commit grouping.
- [`docs/plans/symphony-inspired-improvements-plan.md`](symphony-inspired-improvements-plan.md)
  — same "mechanisms not policy" framing.

CLAUDE.md sections that bind this work (cited verbatim where they
constrain the design):

- **§5 Minimal Change Set** — "Extend existing mechanisms — never compete
  with them." Items 1–8 extend an existing prompt file or script
  placeholder; item 9 adds a single new `prompts/overlays/` subdirectory
  loaded through the existing `scripts/render_prompt.sh` placeholder
  pipeline.
- **§6 Backward Compatibility / Naming Immutability** — no identifier is
  renamed. Item 2 adds a new `{{MODEL_FAMILY_OVERLAY}}` placeholder
  alongside the existing `{{WORKFLOW_EDIT_RESTRICTION}}` /
  `{{SEMBLE_PREFETCH}}` / `{{SERENA_TOOL_HINTS}}` placeholders in
  `scripts/render_prompt.sh`; every existing placeholder remains
  unchanged. Item 8 adds a new section heading in `agents.md` and does
  not renumber or rename any existing section.
- **§9 Code Style** — every new shell snippet uses TAB indentation;
  every new YAML snippet (item 9's overlay manifest, if YAML is the
  chosen format) uses 2-space indentation.
- **§13 Repository Hygiene** — no writes into `.git/**`.
- **§14 Consumer Repo Registry** — `.github/ai/consumer_repos.json` is
  untouched; no `repository_dispatch` is triggered. Items that adjust
  `workflow-templates/` are explicitly deferred (none of the nine
  adopted items lands a workflow-templates edit; the closest item, item
  6's validate-discover prompt change, lands in `prompts/` only).
- **§15 GitHub API Call Hygiene** — zero new `gh api` / `gh_retry` /
  `curl https://api.github.com/...` callsites. All adopted items
  operate on local prompt text, local scripts, or text already supplied
  by existing wrappers.

## Goals

Each goal is falsifiable by re-reading the named file after the
corresponding item lands. Item numbers map 1:1 to the Implementation
Steps section.

1. **Explore-first gate** — `prompts/mode-plan.txt` and
   `prompts/mode-implement.txt` carry an explicit "before any write,
   exhaust read-only context gathering (grep / glob / read / structured
   search) and record what was inspected" instruction modelled after
   open-agents' `subagents/explorer.ts` read-only role split. Source:
   `packages/agent/subagents/explorer.ts` + `executor.ts` + the
   `docs/agents/architecture.md` "Subagent Pattern" section.
2. **Model-family overlay blocks** — `prompts/overlays/` contains at
   least three family overlay files
   (`gpt.txt`, `claude.txt`, `gemini.txt`, plus a default `other.txt`),
   loaded through a new `{{MODEL_FAMILY_OVERLAY}}` placeholder in
   `scripts/render_prompt.sh` and selected per-reviewer in
   `scripts/review_run_reviewers.sh`. Source:
   `packages/agent/system-prompt.ts` model-family overlay assembly.
3. **Plan-mode six-step gate** — `prompts/mode-plan.txt` ends with an
   explicit "Explore → Clarify → Design → Review → Present → Implement
   readiness" checklist; the plan-phase output is invalid (treat as
   non-CLEAR) when any step is unrecorded. Source:
   `.agents/skills/plan-mode/SKILL.md` six-step process.
4. **Verification-loop discipline** — `prompts/mode-implement.txt`
   names the explicit verification order (`typecheck → lint → tests →
   build`) and the "iterate until all pass; never claim done without
   running verification" rule. Source:
   `packages/agent/system-prompt.ts` "Verification Loop" section.
5. **Read-before-edit + parallel-when-independent + no-surprise-edits-over-3-files**
   — three open-agents system-prompt rules land verbatim in
   `prompts/mode-implement.txt` and `prompts/mode-implement-repair.txt`.
   Source: `packages/agent/system-prompt.ts` core guardrails.
6. **Project-script + package-manager autodetect** —
   `prompts/mode-validate-discover.txt` and
   `prompts/mode-validate-generate.txt` add explicit "consult project
   scripts (AGENTS.md / package.json / Makefile / scripts/) before
   suggesting generic verification commands" and "detect the package
   manager from lock files (bun.lockb / pnpm-lock.yaml / yarn.lock /
   package-lock.json) before invoking npm/yarn/pnpm/bun directly".
   Source: open-agents `docs/agents/lessons-learned.md` General /
   Tooling section.
7. **Auto-approve allowlist + destructive-pattern justification** —
   `unattended_system_instructions.md` gains a short "safe-by-default
   bash heuristics" block listing the auto-approved read-only commands
   (`ls`, `find`, `grep`, `git status`, `git diff`, `git log`, `pwd`,
   `echo`, `cat`-equivalents) and the destructive patterns requiring
   explicit justification (`rm`, `mv`, `cp`, `chmod`, `chown`, `sudo`,
   pipes/redirects/chaining into destructive commands, package-manager
   installs, force-push variants, `git reset --hard`, `git clean -f`,
   `git checkout -- .`). The block is advisory — codex-cli's
   `--approval-mode` flag still governs runtime enforcement. Source:
   `packages/agent/docs/approval-system.md`.
8. **Categorised operational lessons section in `agents.md`** —
   `agents.md` gains an "Operational lessons learned (categorised)"
   section grouping stable knowledge from
   `probably_unnecessary_but_read_if_stuck.md` and current production
   incidents under six categories matching open-agents'
   `lessons-learned.md` taxonomy adapted for our domain:
   **General / Tooling**, **codex-cli quirks**,
   **OpenRouter / prompt-cache**, **GitHub API rate-limits**,
   **Memory subsystem**, **Validation harness Docker lifecycle**.
   Each category lists the stable invariants and pointers (not
   prose summaries) so the section stays bounded.
   Source: open-agents `docs/agents/lessons-learned.md` taxonomy.
9. **Tier-based prompt compaction helper** —
   `scripts/openrouter_prompt_cache.py` gains a helper
   `compact_if_over_budget(sections, budget)` that drops the lowest-tier
   sections in order until the assembled prompt fits a configurable
   token budget (default `OPENROUTER_PROMPT_BUDGET_TOKENS=160000`). The
   helper is opt-in; existing prompt assembly paths are not rewired by
   default. Source:
   `packages/agent/context-management/aggressive-compaction-helpers.ts`.
   This item overlaps with the gsd plan's "phase-prompt size budgets"
   item (`docs/plans/gsd-inspired-improvements-plan.md` Goal 1); if the
   gsd plan ships first, this item degrades to a thin wrapper around
   the gsd budget machinery instead of a standalone helper. The overlap
   is intentional — both plans converge on the same compaction
   primitive — and the implementation step below records the
   dependency-aware shape.

## Non-goals

This plan deliberately does NOT cover:

- Any restructure of `docs/` into `docs/agents/architecture.md` +
  `docs/agents/lessons-learned.md` + `docs/agents/code-style.md` style
  three-doc split. The user excluded `docs/` from in-scope surfaces.
- Any addition to `.claude/skills/`, `.claude/agents/`,
  `.claude/hooks/`, or `.claude/commands/`. The user excluded the
  interactive Claude surface. Open-agents' `.agents/skills/` mechanism
  (with `SKILL.md` discovery / loader / triggers — files
  `discovery.ts`, `loader.ts`, `types.ts`) is therefore not adopted in
  this plan.
- Any port of open-agents' subagent registry as a literal Codex-CLI
  tool. Codex CLI has no `task` tool; subagent split is realised here
  only as prompt-level guidance (item 1) and reviewer-overlay routing
  (item 2). A literal subagent registry is out of scope.
- Any change to consumer wrappers under `workflow-templates/`.
- Any rename or removal of an existing identifier (§6).
- Any new `gh api` / `gh_retry` / `curl https://api.github.com/...`
  callsite (§15).

## Constraints

- **§5 Minimal Change Set** — items 1–8 extend existing prompt or
  script surfaces; item 9 adds a single helper alongside existing
  prompt-cache instrumentation. No format/type/unrelated-logic change.
- **§6 Backward Compatibility / Naming Immutability** — every existing
  prompt filename, script filename, placeholder name, env-var name,
  log-prefix name, and section heading is preserved. New items add
  new identifiers (`{{MODEL_FAMILY_OVERLAY}}`,
  `prompts/overlays/<family>.txt`, `OPENROUTER_PROMPT_BUDGET_TOKENS`,
  `compact_if_over_budget`) that did not previously exist.
- **§9 Code Style** — TAB indentation in new shell snippets; 2-space
  indentation in any new YAML.
- **§10 MongoDB Rules** — N/A. No collection or index touched.
- **§13 Repository Hygiene** — no writes into `.git/**`.
- **§14 Consumer Repo Registry** — N/A. No workflow-templates change,
  no `repository_dispatch`.
- **§15 GitHub API Call Hygiene** — N/A. Zero new API callsites.

## Approach

Pick the open-agents mechanisms that survive the projection from a
TypeScript / Vercel / Better-Auth / Next.js stack onto our codex-cli
unattended pipeline, and adopt them as additive prompt and script
extensions only. Specifically:

- **Adopt at the prompt layer** when the mechanism is a behavioural
  rule (verification loop, explore-first, read-before-edit,
  package-manager autodetect, plan-mode six-step). Six of the nine
  items land in `prompts/mode-*.txt`.
- **Adopt at the script layer** when the mechanism is mechanical
  (model-family overlay routing, tier-based compaction helper). Two
  of the nine items land in `scripts/`.
- **Adopt at the doc-policy layer** when the mechanism is a stable
  fact reviewers should see (bash safety heuristics, operational
  lessons taxonomy). Two of the nine items land in
  `unattended_system_instructions.md` and `agents.md` respectively
  (item 7 is the bash heuristics block; item 8 is the categorised
  lessons section).

Alternatives considered and rejected:

- **Literal subagent registry as a Codex-CLI tool surface.** Codex CLI
  has no extension point for in-prompt subagent dispatch the way
  Claude's `task` tool does; emulating this with `codex exec`
  subprocess chains would balloon the GitHub Actions runtime budget
  and conflict with the explorer/executor split's lifecycle
  expectations. Rejected; prompt-level guidance (item 1) is the only
  defensible adoption.
- **Skills system at `.claude/skills/SKILL.md` with discovery / loader
  / triggers.** Out of scope per Q3 — the user excluded the `.claude/`
  surface. The mechanism is well-suited to a separate follow-up plan
  if/when the interactive surface comes back in scope.
- **Doc restructure under `docs/agents/`.** Out of scope per Q3 — the
  user excluded the `docs/` surface. The `agents.md` operational
  lessons section (item 8) is the in-scope substitute.
- **Path-security helper from `packages/agent/tools/path-security.ts`
  as a new `scripts/path_security.py`.** Our scripts already operate
  on `${{ github.workspace }}` with no user-supplied paths in the
  hot path. Marginal value, ambient drift risk. Rejected.

## Implementation Steps

Each step is small enough to land as one or two commits. File paths
include the existing line ranges that are extended; new files are
marked `[new]`.

1. **Explore-first gate in plan + implement prompts.**
   - `prompts/mode-plan.txt:1-84` — add a new "Read-only exploration
     sub-phase" block immediately after the existing context-loading
     preamble, listing the read-only operations
     (`grep`, `glob`, file read, `apply_patch -- preview`, manifest
     inspection, lockfile detection) that MUST occur before any
     write-shaped plan emission. Append the explicit "no write-mode
     tools may be invoked during this sub-phase" line.
   - `prompts/mode-implement.txt:1-71` — add a new "Pre-edit
     exploration checklist" block listing the same read-only
     operations, with the rule "every file you intend to edit must
     appear in the exploration log before the first `apply_patch`".
   - Acceptance: both prompts contain the new block; the implement
     prompt's editing rules now reference the exploration log by
     name.
2. **Model-family overlay routing.**
   - `prompts/overlays/` `[new]` directory.
   - `prompts/overlays/gpt.txt` `[new]` — gpt-5.4 / gpt-5.4-mini
     overlay (covers reviewer model `openai/gpt-5.4` and the
     summariser `openai/gpt-5.4-mini`). Emphasises "iterate until
     problem is completely solved; think critically between steps;
     concise post-fix reply".
   - `prompts/overlays/claude.txt` `[new]` — Claude overlay. Emphasises
     `todo_write` discipline, mark-complete-per-item, and the
     CLAUDE.md §0 / §2 STOP-and-ASK contract bridge so a Claude
     reviewer running over an unattended prompt does not silently
     emit a clarification request that the unattended pipeline
     cannot answer.
   - `prompts/overlays/gemini.txt` `[new]` — Gemini overlay. Cap
     non-tool prose at three lines, get straight to action, emit the
     same severity classification labels the reviewer-bundle parser
     expects.
   - `prompts/overlays/other.txt` `[new]` — fallback for third-party
     reviewer models (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`,
     `deepseek/deepseek-v4-pro`, `qwen/qwen3.6-plus`,
     `x-ai/grok-4.1-fast`). Minimal — restates the severity-label
     contract and the "no clarification questions" rule.
   - `scripts/render_prompt.sh:1-50` — add a new
     `{{MODEL_FAMILY_OVERLAY}}` placeholder branch alongside the
     existing `{{WORKFLOW_EDIT_RESTRICTION}}` /
     `{{SEMBLE_PREFETCH}}` / `{{SERENA_TOOL_HINTS}}` branches. The
     placeholder resolves from the optional
     `${MODEL_FAMILY_OVERLAY}` environment variable; empty / unset
     resolves to an empty block (no behaviour change for existing
     callers).
   - `scripts/review_run_reviewers.sh:1140-1165` — extend the
     existing `run_reviewer` per-model setup to pick the overlay file
     by family from the model slug (`openai/*` → `gpt.txt`,
     `anthropic/*` → `claude.txt`, `google/*` or
     `*/gemini-*` → `gemini.txt`, else `other.txt`) and export
     `MODEL_FAMILY_OVERLAY=<contents>` before the
     `render_prompt.sh` invocation. Inline the overlay file body
     into the env var so the render pass does not need to read the
     filesystem; this also keeps per-iteration GitHub Actions runner
     I/O bounded.
   - Acceptance: reviewer-prompt assembly for each reviewer model
     ends with the matching family overlay block; an empty
     `MODEL_FAMILY_OVERLAY` env var leaves the rendered output
     byte-identical to the pre-change baseline (regression-safe).
3. **Plan-mode six-step gate.**
   - `prompts/mode-plan.txt:1-84` — add a new closing section
     "Six-step readiness gate" with the six labels (`EXPLORE`,
     `CLARIFY`, `DESIGN`, `REVIEW`, `PRESENT`, `IMPLEMENT-READY`)
     each requiring a one-line evidence record. State explicitly
     that `STATUS: CLEAR` is invalid unless all six labels carry a
     non-empty evidence line. The clarify-respond loop already
     handles the `CLARIFY` step by routing follow-up questions
     through `orchestrate_clarify_respond` (no new mechanism).
   - `prompts/mode-clarify.txt` — add a one-line back-pointer noting
     the planner's `CLARIFY` step expects question batches in the
     existing `Q1`/`Q2` format. (No new behavior; cross-reference
     only.)
   - Acceptance: the plan prompt's STATUS: CLEAR emission contract
     now requires the six-step evidence block.
4. **Verification-loop discipline in implement prompt.**
   - `prompts/mode-implement.txt:1-71` — add a "Verification loop"
     block stating the explicit order `typecheck → lint → tests →
     build` with the rule "iterate until every step passes; never
     emit a 'done' signal without running verification end-to-end"
     and the cross-reference to the per-repo verification commands
     in the consumer repo's `AGENTS.md` / `package.json` /
     `Makefile`.
   - Acceptance: the implement prompt now enumerates the four
     verification stages in order; the validate phase prompts are
     unchanged (they have their own verification model).
5. **Read-before-edit + parallel + no-surprise-edits rules.**
   - `prompts/mode-implement.txt:1-71` — add three guardrail bullets
     verbatim (with our terminology where it differs):
     - "Always read each file completely before the first `apply_patch`
       on that file; the exploration log from item 1 is the
       authoritative checklist."
     - "Run independent operations in parallel; serialise only when
       there is an explicit dependency (matches CLAUDE.md §16
       parallel-tool-use guidance)."
     - "Never make surprise edits affecting more than three files
       outside the plan's `files_touched` set without flagging the
       expansion in the post-implementation summary."
   - `prompts/mode-implement-repair.txt` — same three bullets,
     adapted (repair is per-file by definition; the "parallel" rule
     is dropped, the "read-before-edit" rule is reinforced as
     "always re-read the failing file before patching").
   - Acceptance: both repair and implement prompts carry the same
     three (or two for repair) guardrails.
6. **Project-script + package-manager autodetect in validate prompts.**
   - `prompts/mode-validate-discover.txt:1-38` — add a "Detection
     order" block: "(1) read consumer repo `AGENTS.md` / `agents.md`
     and `README.md` for documented test commands; (2) read
     `package.json` `scripts` and `Makefile` targets; (3) detect the
     package manager from lock files in the priority order
     `bun.lockb` → `pnpm-lock.yaml` → `yarn.lock` →
     `package-lock.json`; (4) fall back to language defaults
     (`pytest`, `go test`, `cargo test`, `mvn test`) only after the
     above three fail." This makes the discovery deterministic
     instead of model-judgement-dependent.
   - `prompts/mode-validate-generate.txt:1-50` — add a back-pointer
     paragraph: "The detection-order list in
     `prompts/mode-validate-discover.txt` is authoritative; do not
     invent `npm test` calls when `bun.lockb` is present."
   - Acceptance: validate-discover lists the four detection layers
     explicitly; validate-generate cross-references the list.
7. **Bash safety heuristics in `unattended_system_instructions.md`.**
   - `unattended_system_instructions.md` — add a new "Bash safety
     heuristics (advisory)" section listing:
     - **Auto-safe read-only** — `ls`, `find` (without `-delete`),
       `grep`, `git status`, `git diff`, `git log`, `pwd`, `echo`,
       `wc`, `head`, `tail`, `stat`, `file`, `cat`-equivalents.
     - **Destructive — require justification in the same step's
       reasoning** — `rm` (any form), `mv`, `cp` over an existing
       destination, `chmod`, `chown`, `sudo`, `git reset --hard`,
       `git clean -f`, `git checkout -- .`, `git push --force` /
       `--force-with-lease`, package-manager installs
       (`apt-get install`, `pip install`, `npm install`, `bun add`,
       `yarn add`, `pnpm add`, `cargo install`, `go install`).
     - **Pipes / redirects / chaining into a destructive command** —
       evaluate the destructive component under the same rule.
     - State explicitly that this section is advisory; codex-cli's
       `--approval-mode` flag still governs runtime enforcement,
       and the existing `MAX_POST_CODEX_REPAIR_ATTEMPTS` cap still
       applies to repair retries.
   - Acceptance: the new section is present and self-contained; no
     existing section is renumbered.
8. **Categorised operational lessons section in `agents.md`.**
   - `agents.md` — append a new section
     "Operational lessons learned (categorised)" at the end (after
     the existing "Review pipeline consolidator + ledger contract"
     section, so no existing section is renumbered or moved). Six
     subsections, each a short bulleted list of stable invariants
     with `(see <pointer>)` links to the canonical detail in
     `probably_unnecessary_but_read_if_stuck.md` or the relevant
     `scripts/` / `prompts/` file:
     - **General / Tooling** — codex-cli rc=0+empty-stdout regression
       (`openai/codex#11151`), `apply_patch_tool_type: function`
       fix in `scripts/codex_model_catalog.json`, `low` verbosity
       resolution.
     - **codex-cli quirks** — announce-without-emit pattern,
       `include_apply_patch_tool = true` belt-and-suspenders,
       Responses-path interaction.
     - **OpenRouter / prompt-cache** — cache-friendly prompt
       ordering, `OPENROUTER_PROMPT_CACHE_DISABLED` kill switch,
       cache-probe skip for Gemini-family models.
     - **GitHub API rate-limits** — `gh_retry` exponential backoff,
       `TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS` pin-based dedup,
       batched GraphQL helpers
       (`_fetch_candidate_issue_details_graphql`,
       `_fetch_linked_pr_status_graphql`).
     - **Memory subsystem** — `ai-memory` branch single-source,
       fail-open contract, `AI_MEMORY_TELEMETRY` log lines,
       per-PR ledger contract.
     - **Validation harness Docker lifecycle** — `/bin/sh -c` vs
       `-lc` PATH difference, `npm`/`yarn`/`pnpm` wrapper SIGTERM
       exit-code-1 translation, `mongosh` apt-repo absence on
       Debian/Ubuntu defaults.
   - Each subsection entry is a single line; the section is bounded
     to ≤60 lines total to keep `agents.md` searchable.
   - Acceptance: the new section is appended, every bullet has a
     pointer, the file's existing line ranges are unchanged below
     the append point.
9. **Tier-based prompt compaction helper.**
   - `scripts/openrouter_prompt_cache.py` — add a new function
     `compact_if_over_budget(sections, budget_tokens)` that accepts
     an ordered list of `(tier, label, body)` tuples (tier `1` =
     keep-always, tier `3` = drop-first) and a token budget, and
     returns the assembled prompt with the lowest-tier sections
     dropped in order until the assembled prompt fits the budget.
     The function is a pure helper; no existing call paths are
     rewired automatically.
   - Add `OPENROUTER_PROMPT_BUDGET_TOKENS` env-var default
     (`160000`) and the corresponding row in `README.md`'s env-var
     table (under the existing OpenRouter cache instrumentation
     section). Document the helper's `(tier, label, body)` shape in
     the function docstring per CLAUDE.md §15's
     "document the batching contract" rule (the helper is not a
     batching helper, but the same documentation discipline applies
     to keep future callers informed).
   - Add a corresponding `tests/test_prompt_compaction.py` `[new]`
     covering: (a) empty input → empty output, (b) under-budget
     input → unchanged output, (c) over-budget input → tier-3
     sections dropped first, (d) extreme over-budget → only tier-1
     sections retained.
   - If
     `docs/plans/gsd-inspired-improvements-plan.md` Goal 1 ships
     first, this item degrades to wiring the gsd budget machinery
     into the same helper signature. The tests do not need to be
     duplicated.
   - Acceptance: the helper is callable from
     `scripts/openrouter_prompt_cache.py`; the test file passes
     under `pytest tests/test_prompt_compaction.py`.

## Files & Modules

New files:

- `prompts/overlays/gpt.txt` `[new]`
- `prompts/overlays/claude.txt` `[new]`
- `prompts/overlays/gemini.txt` `[new]`
- `prompts/overlays/other.txt` `[new]`
- `tests/test_prompt_compaction.py` `[new]`

Edited files:

- `prompts/mode-plan.txt` — items 1, 3.
- `prompts/mode-implement.txt` — items 1, 4, 5.
- `prompts/mode-implement-repair.txt` — item 5.
- `prompts/mode-validate-discover.txt` — item 6.
- `prompts/mode-validate-generate.txt` — item 6.
- `prompts/mode-clarify.txt` — item 3 (one-line cross-reference only).
- `scripts/render_prompt.sh` — item 2 (new placeholder branch).
- `scripts/review_run_reviewers.sh` — item 2 (overlay routing in
  `run_reviewer`).
- `scripts/openrouter_prompt_cache.py` — item 9 (helper + env-var
  default).
- `agents.md` — item 8 (appended section).
- `unattended_system_instructions.md` — item 7 (appended section).
- `README.md` — item 9 (env-var table row for
  `OPENROUTER_PROMPT_BUDGET_TOKENS`).

No deletions. No renames. No identifier changes.

## Data Model / Index Changes

N/A. No MongoDB collection, index, contract, write entrypoint, or
business invariant is touched. §10 does not bind.

## Tests

- **Unit** — `tests/test_prompt_compaction.py` `[new]` covers the
  four cases enumerated in item 9.
- **Regression** — for item 2, a smoke test (manual or scripted)
  confirms that with `MODEL_FAMILY_OVERLAY` unset the rendered
  output of `scripts/render_prompt.sh prompts/mode-implement.txt` is
  byte-identical to the pre-change output. The repo's existing
  reviewer-pipeline self-tests in `tests/` cover the end-to-end
  reviewer fan-out.
- **Manual smoke** — for items 1, 3, 4, 5, 6: kick a low-cost smoke
  run of `clarify.yml` + `plan.yml` + `implement.yml` against a
  trivial scratch issue, verify the new prompt blocks appear in the
  workflow log (codex-cli echoes the prompt). For item 7, no smoke
  is needed — the section is advisory text. For item 8, no smoke
  is needed — the section is read by humans and the workflow-log-
  analysis audit (which scans `agents.md` for pointer integrity).
- **Cross-pipeline integration** — none of the nine items changes a
  workflow YAML or a runtime contract; the existing CI in
  `.github/workflows/ci.yml`, `comprehensive-test-and-release.yml`,
  and `nightly-validation-selftest.yml` continues to run unchanged
  and is the regression backstop.

## Risks & Mitigations

- **Prompt drift inflates context budget.** Items 1, 3, 4, 5, 6 add
  prompt text to four `prompts/mode-*.txt` files. Mitigation: every
  new block is < 30 lines; the per-prompt size budget rule
  enumerated in `docs/plans/gsd-inspired-improvements-plan.md`
  Goal 1 (if shipped) catches over-budget files automatically.
  Pre-merge, manually check `wc -l` per touched prompt file stays
  under 200 lines (current sizes: plan = 84, implement = 71,
  validate-discover = 38).
- **Model-family overlay routing leaks across reviewers.** Item 2
  exports `MODEL_FAMILY_OVERLAY` in `run_reviewer` shell context;
  parallel reviewer subshells must not see each other's overlay.
  Mitigation: existing `run_reviewer` already spawns each reviewer
  under an isolated subshell (the `>&2 &` pattern at
  `scripts/review_run_reviewers.sh:1580`); the new env-var export
  is scoped to that subshell.
- **Six-step gate stalls `STATUS: CLEAR`.** Item 3 makes
  `STATUS: CLEAR` invalid unless six labels are populated; a poorly
  understood task could loop on the gate. Mitigation: the gate is
  prompt-level only — codex-cli does not enforce; the existing
  `MAX_PLAN_TURNS` (if any) and `EDITOR_MAX_WALL` budgets bound the
  loop. Stall recovery (`ORCH_PR_AUTOFIX_FLOW_ENABLED` cascade)
  catches genuine deadlocks.
- **Auto-approve allowlist over-permissive.** Item 7's allowlist is
  advisory; codex-cli's `--approval-mode` is the real gate. Risk is
  bounded.
- **Compaction helper drops critical context.** Item 9 drops lowest-
  tier sections first. Mitigation: only callers that opt in are
  affected; callers must explicitly tag sections with tiers; tier-1
  (keep-always) is the safe-default for callers that don't tier.
  The test cases in `tests/test_prompt_compaction.py` cover the
  drop ordering. ACCEPTED — the helper is opt-in and unused by
  default at merge time.
- **Overlap with the gsd-inspired plan.** Item 9 overlaps with that
  plan's Goal 1. Mitigation: the implementation step explicitly
  records the dependency; if gsd ships first, item 9 degrades to a
  thin wrapper. ACCEPTED.
- **Categorised lessons section in `agents.md` rots.** Item 8 adds a
  pointer-only section; the pointers can rot when underlying files
  move. Mitigation: every pointer is a relative path under
  `scripts/` / `prompts/` / `probably_unnecessary_but_read_if_stuck.md`,
  which are stable per §6; the periodic `workflow-log-analysis.yml`
  audit can be extended (not in this plan — flagged as a follow-up)
  to grep the section for dead pointers.

## Rollout

- **No feature flag, no dark launch.** Items 1, 3, 4, 5, 6, 7, 8 are
  prompt / doc edits — they take effect on the next workflow run
  after merge. The risk surface is "model emits a slightly
  different output shape"; the existing reviewer-bundle parser is
  fail-open per `unattended_system_instructions.md` §14 and the
  parser tolerates additional prose.
- **Item 2 is opt-in by env var.** `MODEL_FAMILY_OVERLAY` resolves to
  empty when unset; `scripts/review_run_reviewers.sh` wires the
  selection so it activates on the next run. To stage the rollout,
  ship the placeholder + overlay files first (commit 1) and the
  `review_run_reviewers.sh` wire-up second (commit 2). Roll back
  commit 2 alone if reviewer output regresses.
- **Item 9 is opt-in by caller.** No existing caller opts in at
  merge time; the helper is dormant until a future caller is wired.
  Roll-back is `git revert` of the single commit.
- **No consumer-repo propagation.** §14 does not apply. No
  `repository_dispatch` is triggered.
- **Roll-back path.** Every item is a single commit (or two for item
  2). `git revert <sha>` is the rollback for any individual item.
  No data migrations.

## Open Questions

- **Q-A** — Item 8's "Operational lessons learned" section: should
  the existing
  `probably_unnecessary_but_read_if_stuck.md` content be left in
  place (pointer-only section in `agents.md` is the navigation
  layer) or partially moved into `agents.md` (single-source-of-
  truth, larger `agents.md`)? Recommendation: leave
  `probably_unnecessary_but_read_if_stuck.md` in place and keep
  `agents.md` as the pointer-only index. The plan as drafted
  encodes the recommendation; the reviewer can flip it pre-merge.
- **Q-B** — Item 2's overlay file format: should the overlays be
  plain text (current draft) or YAML-keyed sections so a single
  overlay file could carry multiple-model entries? Recommendation:
  plain text — the existing `prompts/mode-*.txt` files are plain
  text, matching the convention keeps `scripts/render_prompt.sh`'s
  placeholder substitution mechanism unchanged.
- **Q-C** — Item 9's default `OPENROUTER_PROMPT_BUDGET_TOKENS` value:
  the current default 160 000 is roughly 60% of gpt-5.4's 272k
  context. Should the default be higher (200 000 = ~75%) or lower
  (120 000 = ~45%) to leave more room for model thinking? Surface
  for the reviewer; no implementation blocker.

## References

- Upstream repo: <https://github.com/vercel-labs/open-agents>.
- Upstream files surveyed (canonical raw URLs are
  `https://raw.githubusercontent.com/vercel-labs/open-agents/main/<path>`):
  - `README.md`
  - `docs/agents/architecture.md`
  - `docs/agents/code-style.md`
  - `docs/agents/lessons-learned.md`
  - `docs/agents/react-best-practices-audit.md`
  - `docs/plans/lazy-sandbox-session-creation.md`
  - `packages/agent/system-prompt.ts`
  - `packages/agent/open-agent.ts`
  - `packages/agent/subagents/{explorer,executor,design,registry,types,constants,index}.ts`
  - `packages/agent/tools/{ask-user-question,bash,fetch,glob,grep,read,write,task,skill,todo,path-security,utils,index}.ts`
  - `packages/agent/skills/{discovery,loader,types,index}.ts`
  - `packages/agent/context-management/{aggressive-compaction-helpers,cache-control,index}.ts`
  - `packages/agent/docs/approval-system.md`
  - `.agents/skills/code-review/SKILL.md`
  - `.agents/skills/plan-mode/SKILL.md`
  - `.agents/skills/vercel-react-best-practices/SKILL.md`
  - `.github/workflows/ci.yml`
- Internal precedents:
  - [`docs/plans/awesome-claude-code-learnings-plan.md`](./awesome-claude-code-learnings-plan.md)
  - [`docs/plans/gsd-inspired-improvements-plan.md`](./gsd-inspired-improvements-plan.md)
  - [`docs/plans/apply-ai-tools-learnings-plan.md`](./apply-ai-tools-learnings-plan.md)
  - [`docs/plans/symphony-inspired-improvements-plan.md`](symphony-inspired-improvements-plan.md)
- Project constraints cited:
  [`CLAUDE.md`](../../CLAUDE.md) §5, §6, §9, §10, §13, §14, §15, §16.
- Repo architecture facts:
  [`agents.md`](../../agents.md).
- Codex-CLI / OpenRouter context:
  [`README.md`](../../README.md) (OpenRouter prompt cache,
  reviewer-panel models, `WORKFLOW_EDITOR_MODEL` resolution).
