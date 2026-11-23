# gstack Learnings — Applying Garry Tan's `gstack` Lessons to `coding-workflows`

## Archived status

This file is the canonical completed-plan record for tracking issue `#3496`. The closeout summary below reflects a re-audit of the shipped repository state on 2026-07-08 UTC; the historical plan text that follows is preserved for context, and where it conflicts with the closeout summary, the closeout summary is authoritative.

## Closeout summary

All sixteen phases (A–P) shipped, flag-gated, and test-covered, re-audited against `origin/main` on 2026-07-08 UTC — including the dedicated security-audit workflow, slop-scan reviewer context, lite/standard/full reviewer tiers, the Diataxis docs-coverage lens, and the AGENTS.md materiality companion finding.

Two structural deviations from the plan text (not gaps): Phase I is implemented inline in `scripts/review_run_reviewers.sh` rather than a standalone `review_tier_resolver.sh`, and several flags (Phase F `WORKFLOW_RETRO_ENABLED`, Phase J `SECURITY_AUDIT_ENABLED`, Phase K `SLOP_SCAN_ENABLED`) now default `true`, ahead of the `false` acceptance-window the plan's rollout section specified.

---

## Summary

Garry Tan's `gstack` (https://github.com/garrytan/gstack) is a Claude Code
skill toolkit: 23 slash-command "specialists" (CEO / Eng Manager /
Designer / QA / Release Engineer / Debugger / CSO / Doc Engineer / …)
plus an explicit builder ethos (**Boil the Lake**, **Search Before
Building**, **User Sovereignty**, **Iron Law of Investigation**). Its
target is the interactive single-developer workflow; ours is the
unattended GitHub-Actions pipeline. The mechanics don't transfer — but
the prompt-shaping principles, operational-signal patterns, quality
gates, and documentation discipline do. This plan inventories every
gstack idea that survives the interactive-vs-unattended translation,
classifies each as **already done** / **partial** / **gap**, and
proposes flag-gated, fail-open implementation phases for the gaps. All
changes that propagate through `workflow-templates/` will follow
CLAUDE.md §14 against the 11 consumer repos in
`.github/ai/consumer_repos.json`.

## Context

### Source repository

`garrytan/gstack` — README:
<https://github.com/garrytan/gstack/blob/main/README.md>; CLAUDE.md:
<https://github.com/garrytan/gstack/blob/main/CLAUDE.md>; ETHOS.md:
<https://github.com/garrytan/gstack/blob/main/ETHOS.md>; AGENTS.md:
<https://github.com/garrytan/gstack/blob/main/AGENTS.md>. gstack is
MIT-licensed; mechanical-pattern reuse (prompt structure, phase
names, decision tables) is permitted without attribution but we will
credit the source in CHANGELOG entries that ship gstack-derived
mechanics.

The relevant gstack surface for our pipeline:

- **Ethos docs.** `ETHOS.md` codifies three principles: *Boil the Lake*
  (the marginal cost of completeness is near-zero with AI coding —
  prefer the complete implementation over the 90 % shortcut when the
  delta is ~70 LOC), *Search Before Building* (three layers of
  knowledge — tried-and-true / new-and-popular / first-principles —
  search before you design), *User Sovereignty* (AI recommends, the
  user decides — agreement between models is signal, not mandate).
- **Persona-driven skills.** Every workflow skill carries a named
  specialist persona — `/plan-ceo-review` "rethinks the problem,"
  `/plan-eng-review` "locks architecture, data flow, edge cases, and
  tests," `/investigate` "Iron Law: no fixes without investigation,
  trace data flow, test hypotheses, stop after 3 failed fixes,"
  `/cso` "OWASP Top 10 + STRIDE threat model with 17 false-positive
  exclusions and 8/10+ confidence gate," `/document-release` builds a
  Diataxis coverage map (tutorial / how-to / reference / explanation).
- **Operational hygiene.** `/retro` weekly retrospective with
  per-person breakdowns, shipping streaks, test-health trends;
  `/learn` cross-session memory with prune/search/export; `slop-scan`
  AI-code-pattern detector with curated "what NOT to fix" list to
  avoid linter gaming; CHANGELOG release-summary discipline (2-line
  headline + lead paragraph + numbers table + audience closing,
  voice rules forbidding AI vocabulary).
- **Cost-aware execution.** Two-tier eval system — `gate` tests
  (deterministic, free, CI-default) vs `periodic` (paid LLM-judge,
  weekly cron); diff-based test selection (`touchfiles.ts`) so a
  doc-only PR doesn't trigger E2E spend.
- **Safety primitives.** `/freeze` directory-scope edit-lock,
  `/careful` destructive-command warning, `/guard` combined.

### Current state in this repo

`coding-workflows` is a thick GitHub-Actions pipeline with substantial
existing alignment with gstack philosophy and several features gstack
does not have an analog for. The relevant prior art:

- **Phased pipeline.** `clarify` → `clarify-respond` → `plan` →
  `implement` → `implement-diagnose` / `implement-repair` →
  `review_autofix` (5-reviewer panel + consolidator + editor + judge)
  → `validate` (Docker harness + self-heal) → `workflow-log-analysis`
  (`agents.md:17-49`). Each phase has its own `prompts/mode-*.txt`
  and runs codex-cli unattended.
- **Multi-model review.** Five third-party reviewer slugs
  (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`,
  `deepseek/deepseek-v4-pro`, `qwen/qwen3.6-plus`,
  `x-ai/grok-4.1-fast`) run in parallel; the consolidator
  (`prompts/review-consolidator.txt`) merges findings under seven
  classification lenses (Security & Input Validation, Correctness &
  Logic, Concurrency / Races / Idempotency, Error Paths & Edge Cases,
  Performance & Resource Use, Index-Contract / DB Rules, Naming /
  Backward Compatibility) — already a persona-per-lens design
  conceptually adjacent to gstack's specialist-skills approach.
- **Floor rules.** `scripts/review_floor_rules.sh` promotes same-file,
  nearby findings from ≥2 distinct reviewers into
  `FLOOR_MULTI_REVIEWER`; gstack has no equivalent (single-reviewer
  per skill), so we're ahead on cross-model agreement signal.
- **Per-PR ledger.** `.ai/review_issue_ledger/pr-<N>.txt` with states
  `NEW | PERSISTING | FIXED | RESURGENT | accepted-residual` provides
  re-review continuity across autofix iterations (`agents.md:176-178`).
- **AI memory.** `scripts/memory_helpers.sh` + `scripts/ai_memory.py`
  persist run events, decisions, plans, review findings, and
  validation outcomes to the dedicated `ai-memory` git branch; prior
  context is retrieved and injected between the static prompt prefix
  and dynamic content. Telemetry: `AI_MEMORY_TELEMETRY: {…}` log
  line on every operation.
- **Cost-aware skip paths.** `AUTOFIX_SKIP_DOC_ONLY` (case-insensitive
  doc-file glob set with `[force-review]` override), `AUTOFIX_SKIP_MAX_ADDITIONS`
  / `AUTOFIX_SKIP_MAX_DELETIONS` (size-threshold branch),
  `AUTOFIX_SKIP_SELF_TRIGGERED` (synchronize-event de-spend) are all
  already in place — gstack's `gate`/`periodic` tier concept is partly
  implemented, but unevenly.
- **Stable log prefixes.** `agents.md:130-147` lists the contractual
  log keys (`LABEL_REPAIR*`, `AUTOFIX_*`, `AI_PHASE_FAILURE_V1`,
  `SEMBLE_*`, `SERENA_*`) that workflow-log-analysis depends on.
- **Memory maintenance.** `memory_maintenance.yml` runs monthly
  compaction and archival.
- **Validate self-heal.** `MAX_SELF_HEAL_ATTEMPTS=2` in-process
  prompt-file rewriting after validation failure.
- **`BLOCKED:` emission contract** — `unattended_system_instructions.md`
  §2 prescribes `BLOCKED: <reason>` instead of pausing for a human;
  matches gstack's "ask, don't guess" Confusion Protocol *in spirit*
  but adapted for unattended execution.
- **CLAUDE.md §0 / §2 / §12.** Interactive sessions already have
  STOP-and-ASK, batched Q-ID clarification format, and proactive PR
  review scope — close to gstack's User Sovereignty + Confusion
  Protocol.

The 16 phases below (A–P) target the **gaps**, not the already-done
bits. Where gstack's lesson is already implemented, the plan calls it
out under §"Where we're already ahead" so reviewers can confirm rather
than re-litigate.

### Sibling docs and prior plans

- `agents.md` — operator-facing facts about the current pipeline,
  reviewer model list, stable log prefixes, ledger contract.
- `CLAUDE.md` — interactive-session rules (§5 Minimal Change Set —
  *tension with gstack's Boil the Lake; see Phase A*, §6 naming
  immutability, §10 MongoDB contracts, §14 consumer-repo propagation,
  §15 GitHub API hygiene, §16 task delegation by cheapest model, §17
  preferred tools).
- `unattended_system_instructions.md` — codex-cli rules the
  `clarify`/`plan`/`implement`/`review_autofix`/`validate`/etc.
  pipelines actually read at runtime; any reviewer or consolidator
  prompt change must be reflected here or in `agents.md`, not just
  in `CLAUDE.md`.
- `docs/completed/ai-code-review-learnings-plan.md` — sibling plan
  applying Cloudflare's `ai-code-review` lessons. Phase E here
  (Eng-Manager plan-template enrichment) overlaps that plan's
  "approval rubric biased toward `approved_with_comments`" item —
  they target different phases (this one: `mode-plan.txt`; sibling:
  `prompts/mode-judge*.txt`), so no flag-namespace collision is
  expected, but reviewers should sanity-check.
- `docs/completed/judge-loop-and-reissue-plan.md` (shipped) — covers
  judge-in-loop, sticky findings, typed rejections; Phase H
  here (lessons-learned capture from consolidator/judge) overlaps
  the shipped sticky-findings work — same files (`scripts/review_consolidate.sh`,
  `prompts/mode-judge.txt`), different aim (this one writes to
  `ai-memory`, that one writes to the ledger), so the two changes
  can land independently but should share an env-var prefix.
- `docs/plans/symphony-inspired-improvements-plan.md` — sibling plan
  applying Symphony's lessons. Phase O here (skill modularity
  refactor of `prompts/`) overlaps Symphony's "strict prompt
  rendering" item — Symphony focuses on the templating engine; this
  plan focuses on factoring out common preludes. Both can land,
  shared engine recommended (Phase O notes this).

## Goals

- **Inventory every gstack idea** that survives the
  interactive-→-unattended translation, classify as already-done /
  partial / gap, and rank by impact × risk-adjusted reach.
- **Adopt the prompt-shaping principles** (Boil the Lake, Search
  Before Building, User Sovereignty, Iron Law of Investigation) in
  the unattended-system rules and in the relevant `prompts/mode-*.txt`
  files, *with explicit reconciliation against existing CLAUDE.md
  §5 (Minimal Change Set) tension*.
- **Sharpen phase prompts via persona naming** without breaking the
  existing role contracts in `unattended_system_instructions.md` §15.
- **Strengthen operational signal** with per-cycle retro outputs,
  memory-hygiene CLI commands, and structured lessons-learned
  capture — built on the existing `ai-memory` branch + telemetry,
  no new persistence layer.
- **Reduce per-PR LLM spend** via diff-content-aware reviewer
  routing (extending the existing `AUTOFIX_SKIP_*` skip paths into a
  three-tier `lite` / `standard` / `full` reviewer model).
- **Add dedicated quality gates** the per-PR review pipeline can't
  efficiently cover: periodic OWASP/STRIDE security audit,
  AI-code-pattern (slop-scan) detection, per-issue directory edit-lock.
- **Improve documentation discipline** with a Diataxis coverage map,
  CHANGELOG release-summary style guide, AGENTS.md materiality
  check, and a shared prompt-prelude refactor.
- **Every new behaviour is feature-flagged with fail-closed default
  matching today's behaviour**; flips to default-on happen only after
  the chunk's acceptance criteria are met on the self-test matrix.

## Non-goals

- **Importing gstack as a literal dependency.** gstack is a Claude
  Code skill toolkit; our pipeline runs codex-cli in GitHub Actions
  with no human in the loop. Direct skill installation is not the
  goal.
- **Reproducing browser-based features.** `/qa`, `/browse`,
  `/design-*`, `/open-gstack-browser`, `/pair-agent`, `/scrape`,
  `/benchmark` (page-load) all require a real Chromium under
  developer control; our validate harness runs in Docker on
  short-lived runners and tests programmatic correctness, not visual
  QA. Out of scope per the user's Q4 answer.
- **Claude-Code-interactive-only features.** `/office-hours`'s 6
  forcing questions, `/careful` / `/freeze` / `/guard` session-level
  toggles, `/context-save` / `/context-restore` cross-session resume
  — these all assume a human-in-the-loop slash-command session.
  Out of scope per the user's Q4 answer. Where the *principle* is
  portable (e.g., scope-lock per-issue via a label, Confusion
  Protocol as `BLOCKED:` emission), the plan adapts it; where it
  isn't, it's explicitly skipped.
- **Developer-machine state.** Anything backed by `~/.gstack/` or
  a Conductor-style worktree taste profile is out of scope; our
  state lives on the `ai-memory` git branch.
- **Multi-AI-host plumbing.** gstack supports 10 hosts (Claude Code,
  Codex CLI, Cursor, OpenCode, Factory, Slate, Kiro, Hermes, GBrain,
  OpenClaw). Our pipeline is standardized on codex-cli with
  OpenRouter as the provider gateway; the third-party reviewer
  models are accessed through the same channel. Multi-host adapter
  refactors are out of scope.
- **Rebuilding the review consolidator's 7 lenses into named
  specialist personas.** The 7 lenses in
  `prompts/review-consolidator.txt` are already a persona-per-lens
  design; renaming them risks a §6 backward-compatibility break for
  the log-prefix consumers and the existing per-PR ledger format.
  Phase C limits persona naming to *new* prose in the role header
  block, leaves the 7-lens classification system intact.

## Constraints

- **§6 naming immutability.** Existing identifiers (env var names,
  log prefixes, ledger states, label-repair categories, stable log
  prefixes from `agents.md:130-147`, prompt-file names under
  `prompts/`) MUST stay byte-for-byte stable. Any phase that
  proposes a rename adds the new name *alongside* the old one,
  accepts both inputs, preserves old outputs.
- **§10 MongoDB rules.** No collection or index changes are
  proposed by this plan. If a future phase adds a memory-record
  schema (Phase G, Phase H), it lands on the `ai-memory` git branch
  using the existing record-schema pattern (`workflow_log_analysis_cache.v1.json`
  precedent) — not a MongoDB collection.
- **§14 consumer-repo registry.** Eleven repos in
  `.github/ai/consumer_repos.json` receive `repository_dispatch`
  events on `@stable` release. Phases that ship prompt or workflow
  template changes propagate via the existing `update_workflows.yml`
  daily cron + `repository_dispatch` mechanism — no new propagation
  channels.
- **§15 GitHub API call hygiene.** Phases that add `gh api` calls
  (Phase F retro output, Phase J periodic security audit, Phase Q
  reviewer routing classifier) MUST justify the new call surface
  and reuse batched helpers
  (`_fetch_candidate_issue_details_graphql`,
  `_fetch_linked_pr_status_graphql` in
  `scripts/orchestrate_poll_process.sh`); cycle-local caches
  (`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`,
  `_candidate_details_json`) are first-class.
- **§16 task delegation tier.** Subagent spawns in the plan use the
  cheapest model that can handle the task; `gpt-5.4-mini` for
  retro summarisation (Phase F) and slop-scan-style structured-output
  detectors (Phase K).
- **Unattended pipeline cannot pause.** `unattended_system_instructions.md`
  §2/§17 forbid interactive STOP-and-ASK; every phase must either
  produce a deliverable, emit `BLOCKED: <reason>`, or write its
  question batch into the artefact. Phases that propose new
  prompts honor this contract.
- **CLAUDE.md §5 ("Minimal Change Set") vs gstack "Boil the Lake"
  tension.** These are not the same principle. §5 governs the
  *interactive PR-review scope* — don't introduce scope, don't
  refactor unrelated code, don't break naming. gstack "Boil the
  Lake" governs *plan-time scope decisions* — when the complete
  implementation costs minutes more than the shortcut, do the
  complete thing. The two are reconcilable: §5 binds the
  *editor/implementer/reviewer*, gstack's principle informs the
  *planner*. Phase A respects this and only loosens scope at plan
  time, not at edit time.
- **Editor cost ceilings.** `EDITOR_MAX_WALL=7800s` (~130 min)
  remains the hard cap; Phase E (Eng-Manager plan template) MUST
  NOT inflate per-issue scope past the existing 60-min
  implementation-time-estimate target (`prompts/mode-plan.txt`
  output-contract line 6).

## Where we're already ahead

The following gstack ideas are already implemented at parity or beyond
in this repo. No new work proposed; the plan documents these so
reviewers can confirm:

| gstack feature | Our equivalent (or better) | Notes |
|---|---|---|
| `/codex` second opinion (1 extra model) | 5-reviewer panel (minimax / kimi / deepseek / qwen / grok) + consolidator + floor rules | We run ≥5 models on every review pass; gstack runs 1 second-opinion model. |
| `/learn` per-project memory | `ai-memory` git branch + `scripts/ai_memory.py` + telemetry | Ours is shared across runs of the same repo; gstack's is per-machine. |
| `/freeze` directory scope-lock (session) | `ALLOW_WORKFLOW_EDITS=false` + `unattended_system_instructions.md` §9 minimal change set + §10 naming immutability | Coarser than gstack's per-skill scope lock — Phase M proposes a finer-grained per-issue label. |
| `/careful` destructive-command warning | `unattended_system_instructions.md` §5 (destructive ops forbidden without explicit instruction) + codex sandbox | Stronger than gstack — ours is enforcement, gstack's is advisory. |
| `/retro` per-week reflection | `workflow-log-analysis.yml` (every PR's CI logs are analysed for failures) | Different cadence (per-PR vs weekly); Phase F adds the weekly-summary surface. |
| `/ship` PR-quality gate before merge | `review_autofix.yml` + judge-merge-decision + `ENABLE_AUTO_MERGE` | Closer integration than `/ship` — auto-merges PRs that pass review. |
| `/investigate` Iron Law (no fixes without RCA) | `prompts/mode-implement-diagnose.txt` + `MAX_POST_CODEX_REPAIR_ATTEMPTS=3` (gstack also caps at 3) | Phase D strengthens the prompt language; the stop-at-3 cap matches. |
| `/document-release` post-ship doc update | Implicit via review autofix + manual README maintenance | Phase N proposes the Diataxis coverage map gap. |
| `/health` code-quality dashboard | `cost_audit.py` + workflow-log-analysis reports | Different focus (cost vs quality) — partial overlap. |
| gstack token-cost telemetry | `INFO: openrouter usage … prompt_tokens=N completion_tokens=N total_tokens=N cache_creation_input_tokens=N cache_read_input_tokens=N` log line on every codex call | Ours is per-call; gstack's is per-skill. We're ahead on observability. |
| gstack `gate` vs `periodic` test tiering | `AUTOFIX_SKIP_DOC_ONLY` + `AUTOFIX_SKIP_MAX_*` + the existing daily / weekly / `@stable` cron tiers | Partial — Phase I generalises this into a `lite` / `standard` / `full` reviewer-panel tier. |
| GitHub API rate-limit alert | `TG_GH_RATELIMIT_ALERT_COOLDOWN_SECS` + Telegram pinned-message dedup | gstack has no equivalent; we're ahead. |
| §15 batched-GraphQL hygiene | `_fetch_candidate_issue_details_graphql`, `_fetch_linked_pr_status_graphql`, cycle-local caches | gstack has no equivalent; we're ahead. |
| Anti-#1469 / merged-issue close sweeps | `ENABLE_CLOSE_MERGED_ISSUES`, `ENABLE_STALL_MERGED_PR_GUARD`, `MAX_IMPL_NOOP_REISSUES` | gstack has no equivalent (no multi-issue orchestrator); we're ahead. |

Items in this table are out-of-scope for new implementation work but
may receive minor prompt-language polish in the persona-rename phase
(Phase C).

## Approach

The 16 phases are grouped into four clusters mirroring the user's Q3
scope answer. Within each cluster, phases are ordered cheapest-and-
safest first so a partial rollout still produces value.

**Cluster 1: Prompt-shaping (phases A–E).** Edit
`prompts/mode-*.txt` and `unattended_system_instructions.md` to
encode gstack's ethos and persona-driven phase framings. Lowest
risk — text-only changes, no code paths affected, fail-open by
construction (the model can ignore prose it doesn't recognise).
Token-cost-positive (sharper prompts → fewer iterations).

**Cluster 2: Operational signal (phases F–I).** Extend
`workflow-log-analysis.yml`, `scripts/ai_memory.py`,
`scripts/review_consolidate.sh`, and the review preflight to emit
new structured signals (retro outputs, lessons-learned records,
per-issue memory-prune candidates, diff-aware reviewer tier
classifier). Medium risk — touches scripts and workflow YAML; every
change is flag-gated with fail-closed default.

**Cluster 3: Quality gates (phases J–L).** Add a dedicated periodic
security-audit workflow, integrate `slop-scan` into the
review-autofix preflight, and ship the per-issue scope-lock label.
Medium-to-high risk — new workflows, new label semantics; each phase
ships gated and fail-open.

**Cluster 4: Structure / discipline (phases M–P).** Add a Diataxis
docs-coverage finding to the review-autofix consolidator, ship a
CHANGELOG style guide, refactor `prompts/` to extract a shared
prelude, and add an AGENTS.md materiality check. Lowest-impact-but-
highest-leverage cluster — pure quality improvements with no
runtime risk.

Each phase below follows the template: **Goal** • **Status** (already
done / partial / gap) • **Files touched** • **Implementation** •
**Flag + default** • **Acceptance criteria**. File line ranges are
approximate at planning time; the implementing PR resolves them
against the actual diff.

---

## Phase A: Boil the Lake + scope-mode forcing question in `mode-plan.txt`

**Goal.** When the marginal cost of completeness is small (≤ ~30
extra LOC, ≤ 10 extra minutes of implementation time, no new
external dependencies), the planner picks the complete option, not
the 90 % shortcut. Combined with a gstack /plan-ceo-review-style
scope-mode classification — *Expansion* / *Selective Expansion* /
*Hold Scope* / *Reduction* — the plan output explicitly names which
mode it took and why, so the implementer and reviewer can audit the
choice.

**Status.** **Gap.** `prompts/mode-plan.txt` enforces a ≤ 60-minute
implementation-time estimate (line 11), which biases hard toward
shortcuts; CLAUDE.md §5 enforces minimal change set for editors and
reviewers. Neither prompt asks the planner to make the scope
decision explicit. The §5/Boil-the-Lake tension is real but
reconcilable: §5 binds the editor/reviewer (don't surprise people
with scope creep at PR time); Boil the Lake binds the planner
(when planning, the completeness math has changed and 100 % is
often the right answer). Phase A surfaces this in the plan output
so the reviewer can audit and the editor never has to guess.

**Files touched.**

- `prompts/mode-plan.txt` (output contract section, ~lines 4–20).
- `agents.md` (add a row to the per-phase reasoning table noting the
  new scope-mode field; or add a new "Planner output contract" subsection).
- `unattended_system_instructions.md` (§2 Bias to Action — clarify
  that "minimum safe change" applies *within the chosen scope mode*,
  not *across* it).

**Implementation.**

1. Add to `prompts/mode-plan.txt` output contract a new line 7:
   `Scope-mode: <Expansion | Selective Expansion | Hold Scope | Reduction>`
   with a one-paragraph justification block immediately after.
2. Add to the same prompt's rules section a "Boil the Lake forcing
   question": *"If shipping the complete implementation costs ≤30 LOC
   more and ≤10 minutes more than the shortcut, and neither adds a
   new dependency nor a new external interface, pick the complete
   option and document it in the scope-mode justification. Cite
   §1 Core Priorities: correctness > backward compatibility >
   operational clarity > performance > speed; completeness moves
   correctness."*
3. Add to the same prompt a "Reduction safety net": *"Reduction
   mode is only valid when the original request is genuinely
   ambiguous about scope. If reducing scope, the plan MUST emit
   a clarification Q-ID under §24 plan-phase carve-out asking
   the operator to confirm the reduction."*
4. Add to `agents.md` a one-paragraph clarification that §5 of
   CLAUDE.md binds editors/reviewers; Boil the Lake binds planners;
   no conflict.
5. Update `unattended_system_instructions.md` §2 with a clarifying
   sentence: *"'Minimum safe change' is scoped to the chosen
   scope-mode emitted by the plan phase. Reducing scope past the
   plan is forbidden in implementation."*

**Flag + default.** `PLAN_SCOPE_MODE_REQUIRED` (default `true`).
When `false`, the new `Scope-mode:` output line is optional;
reviewers' consolidator does not gate on its absence. Tightening
to required default-on at the end of acceptance window (see
Rollout).

**Acceptance criteria.**

- Five consecutive plan outputs include the `Scope-mode:` field.
- One plan correctly chooses Expansion mode for a real PR (manual
  audit by reviewing the ai-memory plan records).
- The §5/Boil-the-Lake tension is documented in `agents.md` such
  that a reviewer arriving cold can answer "is loosening the
  implementation scope here a §5 violation?" in <30 seconds.

---

## Phase B: Search Before Building reuse audit in `mode-plan.txt`

**Goal.** Before designing new code, the planner first enumerates
existing similar code in the repo and decides whether to extend or
build new. Reduces duplicate logic across `scripts/*.sh` (current
example: there are ≥3 different `gh_retry` wrapper styles in the
repo; gstack's /plan-eng-review would catch this).

**Status.** **Partial.** Plan phase has `web_search` enabled (per
`unattended_system_instructions.md` §2 "If web search is enabled
… fetch public API docs / RFCs / library reference"). But there is
no explicit "search the codebase for existing solutions first"
forcing function. The implementer phase reads files reactively;
the planner does not enforce a reuse audit.

**Files touched.**

- `prompts/mode-plan.txt` (rules section, after the existing
  read-clarification-answers rule).

**Implementation.**

1. Add to `prompts/mode-plan.txt` rules a "Reuse audit" subsection:
   - "Before proposing new functions or files, enumerate every
     existing implementation of the same capability in the repo.
     Use `grep` / `ripgrep` / Read tool to find candidates."
   - "Layer 1 (tried-and-true): runtime built-ins, existing
     helpers in `scripts/gh_helpers.sh`, `scripts/memory_helpers.sh`,
     `scripts/label_helpers.sh`. Always check these first."
   - "Layer 2 (codebase patterns): batched GraphQL helpers
     (`_fetch_candidate_issue_details_graphql`,
     `_fetch_linked_pr_status_graphql`), cycle-local caches
     (`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`),
     stable log prefixes from `agents.md:130-147`."
   - "Layer 3 (first principles): only when Layers 1 and 2
     genuinely miss the pattern. If you reach Layer 3, the plan
     MUST justify it in the implementation steps."
2. Add an output-contract field `Reuse-audit:` after the Scope-mode
   field from Phase A:
   - `Reuse-audit: extends <existing-name>` (Layer 1 / 2 match
     found) OR `Reuse-audit: net-new (Layer 3) — <justification>`.
3. No `agents.md` change required — this is purely a planner-side
   rule.

**Flag + default.** `PLAN_REUSE_AUDIT_REQUIRED` (default `true`).
When `false`, the `Reuse-audit:` field is optional.

**Acceptance criteria.**

- Two consecutive plans on real PRs identify at least one Layer 1
  or Layer 2 candidate when one genuinely exists (audited from
  ai-memory plan records).
- Zero false-negatives in the first month (plans that ship
  `Reuse-audit: net-new` when an obvious extension candidate
  existed). Tracked via `workflow-log-analysis.yml`.

---

## Phase C: Persona role rewrites across `prompts/mode-*.txt`

**Goal.** Each phase prompt opens with a named specialist persona,
not the generic `Role: <phase>-phase auditor`. Sharper persona →
sharper output. Persona is *prepended* prose; existing role
contract in `unattended_system_instructions.md` §15 is preserved.

**Status.** **Partial.** `prompts/mode-plan.txt:1` says `Role:
planning-phase auditor`. `prompts/mode-clarify.txt:1` says
`Role: clarify-phase auditor`. Generic. The review consolidator's
7 lenses are already specialist-named (Security & Input Validation,
Correctness & Logic, etc.) — Phase C explicitly does NOT touch
those (see Non-goals).

**Files touched.**

- `prompts/mode-clarify.txt` (role header).
- `prompts/mode-plan.txt` (role header).
- `prompts/mode-implement.txt` (role header).
- `prompts/mode-implement-diagnose.txt` (role header — see Phase D
  for joint Iron Law work).
- `prompts/mode-implement-repair.txt` (role header).
- `prompts/mode-validate-generate.txt`,
  `mode-validate-diagnose.txt`,
  `mode-validate-fix-harness.txt`,
  `mode-validate-self-heal.txt`,
  `mode-validate-discover.txt` (role headers).
- `prompts/mode-judge.txt`,
  `mode-judge-review-blocked.txt`,
  `mode-judge-stall-recovery.txt`,
  `mode-orchestrate-poll-judge.txt` (role headers — see Non-goals
  re: consolidator 7-lens preservation).
- `prompts/mode-workflow-analysis.txt`,
  `mode-workflow-audit.txt`,
  `mode-workflow-api-redundancy.txt` (role headers).
- `prompts/conflict-resolver.txt`,
  `integration-sync-conflict-resolver.txt` (role headers).

**Implementation.** Proposed persona names (one-line summary +
two-sentence persona-brief prepended to each prompt):

- **clarify** — "**YC-style office-hours interrogator.** Your job is
  to surface the hidden ambiguity in the request without inventing
  scope. Ask only the questions that would materially change the
  implementation."
- **plan** — "**Eng Manager locking down architecture.** Your job is
  to lock the data flow, edge cases, and tests before code is
  written. Force hidden assumptions into the open."
- **implement** — "**Senior implementer with §5 minimal-change-set
  discipline.** Your job is to land the plan's intent on disk with
  the smallest safe edit. Verify, don't speculate."
- **implement-diagnose** — "**Debugger applying the Iron Law of
  Investigation.** Your job is to trace the failure to its root
  cause before proposing any fix. Stop at 3 failed-fix attempts —
  emit a structured fix-up issue proposal instead." (See Phase D.)
- **implement-repair** — "**Surgical repairer.** Your job is the
  smallest possible fix to the syntax/semantics error in the named
  file. Do not touch other files."
- **validate-generate** — "**QA harness author.** Your job is to
  build the minimal Dockerised validation harness that exercises
  the implementation's contract."
- **validate-diagnose** — "**Validation-failure root-cause analyst.**
  Your job is to attribute the failure to plan vs implementation vs
  environment, with cited file/line evidence."
- **validate-fix-harness** — "**Harness self-healer.** Your job is
  to patch the harness when the failure is in the harness, not the
  implementation."
- **validate-self-heal** — "**Prompt-file self-healer.** Your job is
  to patch one of the four validation prompt files when the failure
  is in the prompt, not the harness."
- **validate-discover** — "**Validation-scope discoverer.** Your
  job is to enumerate what should be tested for this issue's stated
  acceptance criteria."
- **judge** — "**Wave-state judge.** Your job is to classify the
  wave-state as `in_progress` / `complete` / `failed` / `blocked`
  with structured evidence; never modify files."
- **judge-review-blocked** — "**Review-blocked judge.** Your job is
  to decide `merge` / `fix` / `merge_with_followup` / `close_and_reissue`
  for a PR that exhausted autofix iterations."
- **judge-stall-recovery** — "**Stall-recovery judge.** Your job is
  to choose the next stall-recovery action when declarative ladder
  is exhausted."
- **conflict-resolver** — "**Merge-conflict resolver.** Your job
  is to preserve both sides' intent in the resolution. Never
  silently discard either side."
- **workflow-analysis** — "**SRE auditor of workflow runs.** Your
  job is to identify systemic failure patterns and propose
  diagnostic-logging additions."
- **workflow-audit** — "**Workflow integrity auditor.** Your job
  is to verify the workflow files match their documented
  contracts."
- **workflow-api-redundancy** — "**API-hygiene auditor.** Your job
  is to enforce CLAUDE.md §15 — every new `gh api` / `gh_retry`
  call must batch or reuse existing calls."

Personas are **prepended** to each prompt above the existing `Role:`
line; the existing `Role:`/`Output contract`/`Rules` blocks stay
byte-for-byte stable to honor §6 naming immutability and to avoid
disturbing the prompt-cache hash.

**Flag + default.** `PROMPT_PERSONA_PREFIX_ENABLED` (default `true`).
When `false`, no persona prefix is inserted at assembly time
(see Phase O — the persona prefix is rendered by the shared
prelude resolver, not hard-coded into each `mode-*.txt`).

**Acceptance criteria.**

- Every `prompts/mode-*.txt` file has its persona block applied at
  assembly time (verified by unit test in `tests/test_assemble_prompt.py`).
- A/B comparison on 3 representative PRs (a feature, a bug fix, a
  doc-only PR) shows no regression on review/autofix iteration
  count or token cost (≤ 5 % drift either way is acceptable noise).
- The 7-lens classification in `prompts/review-consolidator.txt` is
  *unchanged* (no rename, no deletion, no re-ordering).

---

## Phase D: Iron Law of Investigation in `mode-implement-diagnose.txt`

**Goal.** Encode gstack's "Iron Law: no fixes without investigation,
trace data flow, test hypotheses, stop after 3 failed fixes" in
the implement-diagnose prompt. We already cap fix attempts at 3
(`MAX_POST_CODEX_REPAIR_ATTEMPTS=3` default, matches gstack's
limit) — Phase D names the principle, adds explicit
trace-data-flow + test-hypothesis requirements before the model
proposes a fix.

**Status.** **Partial.** `prompts/mode-implement-diagnose.txt`
already emits structured JSON fix-up-issue proposals (per
`agents.md:30-32`). The post-Codex repair attempts cap at 3.
What's missing: explicit "trace before fix" and "test the
hypothesis before fix" steps in the prompt.

**Files touched.**

- `prompts/mode-implement-diagnose.txt` (rules section).

**Implementation.**

1. Add to `prompts/mode-implement-diagnose.txt` a new top-level
   rule block: *"Iron Law of Investigation: no fix proposals
   without (1) a traced data-flow from the failure point back to
   the root, citing every file/line/function in the trace, and
   (2) a stated hypothesis that the trace supports. The fix-up
   issue's structured output MUST include the trace and the
   hypothesis fields. Without both, emit `BLOCKED: insufficient
   diagnostic evidence` and the orchestrator will dispatch a
   stall-recovery cycle."*
2. Add a new top-level schema field to the fix-up-issue JSON:
   `evidence_trace: [{file, line, function, observation}, …]`
   and `hypothesis: <string>`.
3. Re-affirm the existing 3-attempt cap in the prompt: *"After 3
   failed fixes, do not propose a 4th — emit a structured
   `escalate_to_judge` fix-up issue instead so the wave-state
   judge can decide merge / merge-with-followup / close-and-reissue."*

**Flag + default.** `DIAGNOSE_TRACE_REQUIRED` (default `true`).
When `false`, the new schema fields are optional.

**Acceptance criteria.**

- Five consecutive implement-diagnose runs emit non-empty
  `evidence_trace` and `hypothesis` fields (verified in ai-memory
  records).
- Zero regression on the post-Codex repair success rate (measured
  in `workflow-log-analysis` over the first 30 days).
- The `MAX_POST_CODEX_REPAIR_ATTEMPTS=3` env var, log prefix,
  and behaviour are unchanged (§6 naming immutability).

---

## Phase E: Eng-Manager plan-template enrichment

**Goal.** The plan output gains optional `Data flow:` (ASCII or
prose), `State machines:` (where the change touches a state machine
like the orchestrator's phase machine), and `Failure modes:`
sections. Inspired by gstack /plan-eng-review's ASCII diagrams + test
matrix + failure-mode enumeration.

**Status.** **Gap.** `prompts/mode-plan.txt` output contract has 7
fields (files / functions / data structures / API / risks / testing
/ time estimate) but none structurally surface data-flow diagrams
or state-machine sketches.

**Files touched.**

- `prompts/mode-plan.txt` (output contract section).
- `agents.md` (operator-facing plan-template description, if one
  exists; otherwise add).

**Implementation.**

1. Extend `prompts/mode-plan.txt` output contract with three
   *optional* fields:
   - `Data flow:` — included when the change introduces or modifies
     a multi-step flow. ASCII art OR numbered prose, ≤ 20 lines.
   - `State machines:` — included when the change touches a state
     machine. Name the states, transitions, and the trigger for
     each transition.
   - `Failure modes:` — included for any change with non-trivial
     error paths. Each failure mode lists: trigger, observable
     symptom, recovery path.
2. Add a guard: *"These fields are optional. Do not pad a trivial
   PR with diagrams; do not over-engineer simple changes. Include
   only when the field would genuinely help the implementer or
   reviewer."*
3. Mention in the prompt: *"For changes to the orchestrator phase
   machine (`ai:clarification` → `ai:planning` → `ai:awaiting-approval`
   → `ai:implementing` → `ai:done` → `ai:ready-to-merge` → `ai:merged`),
   the `State machines:` field is required, not optional."*

**Flag + default.** `PLAN_DIAGRAMS_OPTIONAL` (default `true`).
When `false`, the new fields are forbidden (reverts to today's
behaviour). When `true`, fields are model-discretion.

**Acceptance criteria.**

- One plan with a multi-step flow correctly produces a
  `Data flow:` diagram (audited from ai-memory).
- Zero increase in median plan-output token cost (measured over
  30 PRs).
- For orchestrator-phase-machine-touching PRs, `State machines:`
  is present in every plan.

---

## Phase F: Per-cycle retro outputs from `workflow-log-analysis.yml`

**Goal.** Periodic (weekly cron + on-demand `workflow_dispatch`)
retro summary that surfaces: PR throughput, autofix iteration
distribution, judge-cycle frequency, top stall causes, model cost
breakdown, top reviewer-finding categories, prompt-cache hit rate.
Posted as a GitHub Discussions thread or a `ai:retro` issue in the
workflow-source repo. Consumer repos receive a per-repo retro via
the existing `update_workflows.yml` channel (Phase F-2 below).

**Status.** **Partial.** `workflow-log-analysis.yml` already audits
workflow runs per its `mode-workflow-analysis.txt` prompt. The
output is structured but not surfaced as a human-readable retro.
`AI_MEMORY_TELEMETRY` records exist per operation but aren't
aggregated weekly.

**Files touched.**

- `.github/workflows/workflow-log-analysis.yml` (add weekly retro
  step).
- `prompts/mode-workflow-analysis.txt` (extend output contract
  with a `Retro` section).
- `scripts/workflow_retro.py` *(new)* — aggregates 7 days of
  workflow runs into the retro structure.
- `agents.md` (document the new retro output and posting channel).

**Implementation.**

1. Add a `weekly-retro` job to `.github/workflows/workflow-log-analysis.yml`
   gated on `github.event_name == 'schedule'` + a `runs[0].schedule`
   matching the weekly cron (e.g., Mondays 09:00 UTC). Job runs
   `scripts/workflow_retro.py` which:
   - Lists merged PRs in the last 7 days (single batched GraphQL
     query — §15 hygiene).
   - Reads `ai-memory` records for the same window (one
     paginated read; fail-open on missing branch).
   - Aggregates: total PRs, median autofix iterations, judge-cycle
     count, top 5 stall reasons (parsed from `LABEL_REPAIR*` log
     prefixes), prompt-cache hit rate from `INFO: openrouter
     usage … cache_read_input_tokens` lines.
   - Emits a markdown retro to `~/retro-<weekstamp>.md`.
2. Have the next step invoke `mode-workflow-analysis.txt` (cheapest
   tier: `gpt-5.4-mini`, reasoning `medium`) with the markdown as
   input, producing a 1-page "retro narrative" with: top three
   things that worked, top three failure modes, one
   recommendation for the next week.
3. Post the retro as a comment on a dedicated `ai:retro` tracking
   issue (one per workflow-source repo); auto-create it if
   missing. Use the existing `gh_helpers.sh` issue-create path.
4. For each consumer repo in `.github/ai/consumer_repos.json`,
   skip per-repo dispatch in v1 — retro applies to workflow-source
   first. Phase F-2 (deferred to a follow-up plan if Phase F
   ships green) introduces per-consumer retros.
5. Document in `agents.md` the new retro cadence, output channel,
   cost ceiling (~$0.05/week with `gpt-5.4-mini` + `medium`).

**Flag + default.** `WORKFLOW_RETRO_ENABLED` (default `false`
during acceptance window, flipped to `true` after one successful
weekly run). `WORKFLOW_RETRO_MODEL` (default `openai/gpt-5.4-mini`).
`WORKFLOW_RETRO_REASONING` (default `medium`).
`WORKFLOW_RETRO_CRON` (default `0 9 * * 1` — Mondays 09:00 UTC).

**Acceptance criteria.**

- One successful weekly retro posted to the `ai:retro` tracking
  issue.
- Per-retro cost ≤ $0.10 (measured via the existing OpenRouter
  usage line).
- Zero new `gh api` calls per workflow run (the retro job's calls
  amortise across 7 days of runs — well within §15 budget).

---

## Phase G: Memory hygiene CLI in `scripts/ai_memory.py`

**Goal.** Operator-facing CLI subcommands for the `ai-memory` git
branch: `review` (list candidate stale records), `prune` (mark for
archival by next `memory_maintenance.yml` run), `search` (semantic
search via OpenRouter embeddings if enabled, falls back to
keyword), `export` (dump per-issue/per-PR records to JSON for
debugging). Inspired by gstack /learn.

**Status.** **Partial.** `scripts/ai_memory.py` has read/write
operations but no curatorial interface. `memory_maintenance.yml`
runs monthly compaction.

**Files touched.**

- `scripts/ai_memory.py` (add subcommands).
- `scripts/memory_helpers.sh` (no change — subcommands wrap the
  existing Python entry points).
- `agents.md` (document the new CLI surface).

**Implementation.**

1. Add to `scripts/ai_memory.py` the following subcommands:
   - `ai_memory.py review --since=<duration> --schema=<schema-name>`
     — lists candidate records older than `<duration>` (default
     90 days) and emits a summary table.
   - `ai_memory.py prune --record-id=<id> [--record-id=<id> …]`
     — marks records for archival.
   - `ai_memory.py search --query="<text>" --schema=<schema-name>`
     — keyword search by default; if `OPENROUTER_API_KEY` is
     set, embeds the query and finds top-K nearest records.
   - `ai_memory.py export --pr=<N> --issue=<N> --format=json`
     — dumps records for the named scope.
2. The `prune` subcommand respects the existing
   `memory_maintenance.yml` job — it marks records, the existing
   monthly job archives them. No new write path to the ai-memory
   branch outside that channel.
3. Add a `pyproject.toml` / `tests/test_ai_memory_cli.py` covering
   each new subcommand on a fixture branch.
4. Document in `agents.md` under "Memory hygiene" section.

**Flag + default.** No flag — this is operator tooling, opt-in
by invocation. No new persistent behaviour.

**Acceptance criteria.**

- All four subcommands work against a fixture ai-memory branch in
  `tests/test_ai_memory_cli.py`.
- The `prune` subcommand is idempotent (re-marking an already-
  marked record is a no-op).
- The `search` subcommand falls open to keyword search when
  `OPENROUTER_API_KEY` is unset.

---

## Phase H: Lessons-learned capture from consolidator/judge

**Goal.** When the editor applies a fix that *wasn't* in the
original plan, or when the judge classifies a wave-state outcome
unexpectedly, capture a "lessons-learned" memory record. Future
plans for similar issues retrieve these as priming context.
Inspired by gstack /learn's cross-session compounding.

**Status.** **Partial.** Memory records (decisions, plans, code
summaries, review findings, validation outcomes) are captured at
phase boundaries. But there's no explicit "what did we learn that
the plan missed?" record.

**Files touched.**

- `scripts/review_consolidate.sh` (emit lessons-learned record).
- `scripts/review_apply_fixes.sh` (emit lessons-learned record on
  out-of-plan fix).
- `prompts/mode-judge.txt` (extend output contract).
- `prompts/mode-judge-review-blocked.txt` (extend output contract).
- `scripts/ai_memory_lib.py` (new schema:
  `lessons_learned_record.v1.json`).
- `agents.md` (document the new schema).

**Implementation.**

1. Define a new memory schema `lessons_learned_record.v1.json`:
   ```json
   {
     "schema": "lessons_learned_record.v1",
     "issue_number": 123,
     "pr_number": 456,
     "phase": "review_autofix" | "implement" | "judge",
     "lesson_kind": "out_of_plan_fix" | "unexpected_judge_verdict" | "review_finding_outside_plan_scope",
     "lesson_text": "…",
     "tags": ["…"],
     "discovered_at": "2026-05-16T…Z"
   }
   ```
2. In `scripts/review_apply_fixes.sh`, when the editor commits a
   fix targeting a file that was NOT in the original plan's
   "Files likely to change" list, emit a lessons-learned record
   via `ai_memory_lib.py`.
3. In `prompts/mode-judge.txt` and
   `prompts/mode-judge-review-blocked.txt`, extend output
   contract with optional `lessons_learned: [{lesson_kind, lesson_text, tags}]`
   field. Judge emits these when its verdict diverges from the
   plan's stated direction.
4. In `prompts/mode-plan.txt`, add to the priming-context section
   a retrieval step: *"Before drafting, retrieve the 5 most
   recent lessons-learned records for issues touching the same
   files. Treat them as soft priors, not gospel."*
5. Document in `agents.md` under "AI memory schemas" the new
   schema and its retrieval channel.

**Flag + default.** `LESSONS_LEARNED_ENABLED` (default `true`,
fail-open: failures to write the record never block the calling
phase).

**Acceptance criteria.**

- After 14 days, ≥ 5 lessons-learned records exist on the
  `ai-memory` branch.
- Manual audit of 3 plans shows lessons-learned context being
  retrieved and influencing the plan's reasoning.
- Zero phase-level failures attributed to lessons-learned write
  errors (verified by `AI_MEMORY_TELEMETRY: op=write_lessons_learned`
  log filter).

---

## Phase I: Lite / Standard / Full reviewer tier + smart routing

**Goal.** Three reviewer-panel tiers selected by diff content
(directory glob), diff size, and file count:

- **Lite** — single reviewer (lowest-cost slug), no consolidator,
  no judge. For: doc-only PRs ≤ 50 LOC; whitespace-only / formatting-
  only; single-file metadata changes.
- **Standard** — 3-reviewer subpanel + consolidator + editor +
  judge. For: small code PRs ≤ 200 LOC, single-directory scope.
- **Full** — current 5-reviewer panel + consolidator + editor +
  judge + floor rules. For: anything else.

Inspired by gstack's `gate` vs `periodic` test tiering AND by gstack
/review's "smart routing" (CEO doesn't review infra fixes).

**Status.** **Partial.** `AUTOFIX_SKIP_DOC_ONLY` skips review
entirely for doc-only PRs ≤ thresholds; that's a binary skip,
not a tier. The 5-reviewer panel is all-or-nothing today.

**Files touched.**

- `.github/workflows/review_autofix.yml` (new tier-resolver step
  before the reviewer dispatch).
- `scripts/review_tier_resolver.sh` *(new)* — classifies the PR
  diff into one of `lite` / `standard` / `full`.
- `scripts/review_run_reviewers.sh` (read the resolved tier;
  dispatch the matching subset of reviewers).
- `scripts/review_consolidate.sh` (skip if tier is `lite`).
- `agents.md` (document the new tiering).
- `README.md` (add env-var rows: `REVIEW_TIER_RESOLVER_ENABLED`,
  `REVIEW_TIER_LITE_MAX_LOC`, `REVIEW_TIER_LITE_REVIEWER_SLUG`,
  `REVIEW_TIER_STANDARD_MAX_LOC`,
  `REVIEW_TIER_STANDARD_REVIEWER_SLUGS`).

**Implementation.**

1. `scripts/review_tier_resolver.sh` reads the PR diff via the
   existing `gh pr diff` cache and emits one of:
   ```
   REVIEW_TIER=lite REVIEW_TIER_REASON=doc_only_<=50_LOC
   REVIEW_TIER=standard REVIEW_TIER_REASON=code_<=200_LOC_single_dir
   REVIEW_TIER=full REVIEW_TIER_REASON=default
   ```
2. Glob set for `lite`:
   - `*.md`, `*.txt`, `*.rst`, `LICENSE*`, `CHANGELOG*`, `docs/**`
     (matches existing `AUTOFIX_SKIP_DOC_ONLY` glob).
   - Strict size cap: `REVIEW_TIER_LITE_MAX_LOC` (default 50).
3. Standard tier: changes confined to a single top-level directory
   (`scripts/`, `prompts/`, `.github/workflows/`, `tests/`),
   total LOC ≤ `REVIEW_TIER_STANDARD_MAX_LOC` (default 200).
4. Full tier: anything else (multi-directory, > 200 LOC, schema
   touch, contract touch). Default.
5. Per-tier reviewer subset: `lite` uses
   `REVIEW_TIER_LITE_REVIEWER_SLUG` (default `qwen/qwen3.6-plus` —
   cheapest at the planning ref). `standard` uses
   `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` (default
   `minimax/minimax-m2.5,deepseek/deepseek-v4-pro,x-ai/grok-4.1-fast`
   — 3 of the 5).
6. The existing `AUTOFIX_SKIP_*` skip paths remain authoritative
   — `lite` tier is a fallback when skip doesn't apply but the
   change is small enough for cheap review.
7. `[force-review]` PR title / `force-review` label override
   forces `full` tier, preserving the existing override semantics.

**Flag + default.** `REVIEW_TIER_RESOLVER_ENABLED` (default
`false` during acceptance, flipped to `true` after one week of
green dry-runs).

**Acceptance criteria.**

- A doc-only 30-LOC PR runs `lite` tier in ≤ 90 s wall-clock
  (vs ~5 min for `full`).
- A 150-LOC scripts-only PR runs `standard` tier with 3
  reviewers; total cost ≤ 60 % of `full`.
- Zero false-negatives in the first 30 days (a PR runs `lite`
  when it should have been `full`; tracked via review-blocked-
  judge interventions on `lite`-tier PRs).

---

## Phase J: Dedicated periodic security audit workflow

**Goal.** Standalone `security-audit.yml` workflow that runs
against the default branch (NOT per-PR — too expensive) weekly.
Uses OWASP Top 10 + STRIDE threat model framing. Distinct from the
per-PR `review_autofix` security lens — that one is fast and
shallow, this one is slow and exhaustive. Inspired by gstack /cso.

**Status.** **Gap.** Per-PR review_autofix has Security & Input
Validation as one of 7 consolidator lenses, but never runs at
default-branch scope with full repo context.

**Files touched.**

- `.github/workflows/security-audit.yml` *(new)*.
- `prompts/mode-security-audit.txt` *(new)*.
- `scripts/security_audit.sh` *(new)*.
- `agents.md` (document the new workflow and findings channel).
- `README.md` (add env-var rows:
  `SECURITY_AUDIT_ENABLED`, `SECURITY_AUDIT_CRON`,
  `SECURITY_AUDIT_CONFIDENCE_GATE`,
  `SECURITY_AUDIT_FP_EXCLUSIONS`).

**Implementation.**

1. New workflow `security-audit.yml` runs:
   - On weekly cron (`SECURITY_AUDIT_CRON`, default
     `0 8 * * 0` — Sundays 08:00 UTC).
   - On `workflow_dispatch` for ad-hoc runs.
2. The workflow:
   - Checks out the default branch.
   - Runs `scripts/security_audit.sh` which invokes codex-cli with
     `prompts/mode-security-audit.txt` and the repo as context.
3. `prompts/mode-security-audit.txt`:
   - Role persona: "**Chief Security Officer.** Your job is OWASP
     Top 10 + STRIDE threat model audit at default-branch scope."
   - Output contract: a JSON array of findings with `{
     finding_id, owasp_or_stride_category, severity, confidence (1-10),
     file, line, exploit_scenario, recommendation }`.
   - Confidence gate: only findings with `confidence ≥ 8` are
     surfaced (the `SECURITY_AUDIT_CONFIDENCE_GATE` default).
   - False-positive exclusion list: hardcoded set of 10–17 known-
     safe patterns (e.g., the
     `gh_helpers.sh` `gh api` paths are not RCE, the codex
     sandbox is the security boundary not the shell, etc.) —
     captured in `scripts/security_audit_fp_exclusions.json`,
     editable.
4. Findings are posted as comments on a single tracking issue
   (`ai:security-audit`, auto-created if missing) with the
   audit-date as a section header. Each high-confidence finding
   also opens a follow-up issue tagged `ai:security` (capped at
   3/week to avoid noise).
5. Costs: estimate ~$5/week at `xhigh` reasoning over the
   default-branch context; bounded by repo size. Documented in
   `agents.md`.

**Flag + default.** `SECURITY_AUDIT_ENABLED` (default `false`
during acceptance, flipped to `true` after one successful run).

**Acceptance criteria.**

- One weekly security-audit run completes successfully.
- All emitted findings cite a real file/line on the default
  branch (zero hallucinated paths).
- The false-positive exclusion list catches ≥ 5 known-safe
  patterns on the first run.
- Per-audit cost ≤ $10.

---

## Phase K: Slop-scan AI-code-pattern integration

**Goal.** Add `slop-scan`-style detection of AI-generated patterns
that are genuinely worse than human-written, NOT to "pass as
human code" but to catch known AI failure modes (empty catches
around file ops, swallowed `ENOENT` in cleanup paths, redundant
`return await`, typed-exception catch loss). Inspired by gstack's
explicit "what NOT to fix" list — it draws a sharp line between
quality and linter-gaming.

**Status.** **Gap.** Validate already runs `pyflakes` /
`ruff check --select F` on python heredocs in
`validation/**/*.sh`. No AI-code-pattern detector runs on
non-heredoc python or on shell scripts.

**Files touched.**

- `scripts/slop_scan_local.py` *(new)* — minimal Python detector
  for the four patterns gstack calls out, applied to
  `scripts/*.py`, `scripts/*.sh`, `validation/**/*.sh`.
- `.github/workflows/review_autofix.yml` (new preflight step:
  run slop-scan against changed files, attach findings as
  reviewer context).
- `prompts/review-consolidator.txt` (add: "If slop-scan findings
  exist, evaluate each against the 'what NOT to fix' criteria
  below before promoting").
- `agents.md` (document the new preflight step).

**Implementation.**

1. `scripts/slop_scan_local.py` detects:
   - **Empty catch around file ops** — `try: os.unlink(p)\nexcept:
     pass` should be `try: os.unlink(p)\nexcept FileNotFoundError:
     pass\nexcept PermissionError: raise`.
   - **Empty catch around process kills** — `try: os.kill(pid, ...)\nexcept:
     pass` should be `try: ...\nexcept ProcessLookupError: pass\nexcept
     PermissionError: raise`.
   - **Redundant `return await`** — without an enclosing try, the
     `await` is needless ceremony.
   - **Untyped catch hiding errors** — `except: pass` with non-
     trivial body in the try (≥ 5 statements or > 1 file op).
2. The "what NOT to fix" guard set:
   - Best-effort cleanup paths (`safeUnlinkQuiet`-style helpers)
     stay untouched.
   - Chrome-extension-style catch-and-log boundaries stay
     untouched (N/A to our repo, but documented for symmetry).
   - String-match-on-error-message patterns stay untouched.
3. Findings are written to
   `${GITHUB_WORKSPACE}/.ai/slop_scan/findings.json` and attached
   as reviewer context.
4. Consolidator prompt is updated to apply the "what NOT to fix"
   filter before promoting slop-scan findings.

**Flag + default.** `SLOP_SCAN_ENABLED` (default `true`,
fail-open: scan failure never blocks review).

**Acceptance criteria.**

- Slop-scan runs in ≤ 5 s on a typical PR diff.
- On a planted-bug fixture (`tests/fixtures/slop_scan/empty_catch_around_os_unlink.py`),
  the detector emits the expected finding.
- On a planted-not-a-bug fixture (`tests/fixtures/slop_scan/safe_unlink_quiet_cleanup.py`),
  the "what NOT to fix" filter suppresses the finding.

---

## Phase L: Per-issue scope-lock label (`ai:scope:<glob>`)

**Goal.** Operator can set an `ai:scope:<glob>` label on an issue
(e.g., `ai:scope:scripts/orchestrate_*.sh`). The implement phase
refuses to edit files outside the glob. Inspired by gstack /freeze
but ported from session-scope to issue-scope and enforced (not
advisory). Useful for surgical fixes to a known-scope module
where the operator doesn't trust the implementer to stay in
bounds.

**Status.** **Gap.** Today, `ALLOW_WORKFLOW_EDITS=false` is the
only scope control, and it's coarse (`.github/workflows/**`).

**Files touched.**

- `.github/workflows/implement.yml` (new "Resolve scope-lock"
  step before the codex-cli invocation; injects glob into the
  implementer prompt; verifies post-commit that no out-of-glob
  files changed).
- `prompts/mode-implement.txt` (new rule referencing the
  injected glob).
- `scripts/label_helpers.sh` (parser for `ai:scope:<glob>`
  label).
- `agents.md` (document the label semantics).
- `README.md` (add env-var row: `SCOPE_LOCK_LABEL_ENABLED`).

**Implementation.**

1. `scripts/label_helpers.sh` gains `parse_scope_lock_label
   <issue_number> → <glob>` (one batched GraphQL call extending the
   existing per-issue label-fetch helper).
2. In `implement.yml`, after the existing scope-check, add:
   - If the issue carries an `ai:scope:<glob>` label, export
     `IMPLEMENTER_SCOPE_GLOB=<glob>` for the codex-cli run.
3. `prompts/mode-implement.txt` reads `IMPLEMENTER_SCOPE_GLOB`
   from the environment (if set):
   - *"This issue carries a scope-lock: only files matching
     `${IMPLEMENTER_SCOPE_GLOB}` may be edited. If the
     implementation requires editing files outside this glob,
     emit `BLOCKED: scope-lock-violation file=<path>` and the
     orchestrator will dispatch a clarification."*
4. Post-commit verification step in `implement.yml`:
   - `git diff --name-only HEAD~1` → filter against the glob →
     if any file is outside, fail the commit and revert.
5. Document the label semantics: glob format follows `bash
   shopt -s globstar` (e.g., `scripts/**/*.py`,
   `prompts/mode-*.txt`).

**Flag + default.** `SCOPE_LOCK_LABEL_ENABLED` (default `false`
during acceptance, flipped to `true` once one real issue
exercises it).

**Acceptance criteria.**

- An issue with `ai:scope:prompts/mode-clarify.txt` label runs
  the implementer with the scope-lock active; the implementer
  emits BLOCKED if it tries to edit any other file.
- The label parser correctly handles `**` globs.
- §15 — the GraphQL label fetch reuses the existing per-issue
  detail batched query (zero new API calls per implement run).

---

## Phase M: Diataxis docs coverage map in review-autofix consolidator

**Goal.** When a PR introduces a user-visible change, the
consolidator enumerates which Diataxis categories
(*reference* / *how-to* / *tutorial* / *explanation*) should be
touched. Surfaced as a finding (severity `low` by default —
advisory, not blocker) so the editor either updates the relevant
docs in the same PR or explicitly accepts the gap. Inspired by
gstack /document-release's Diataxis coverage map.

**Status.** **Gap.** Today, doc updates are reactive (covered by
the per-PR consolidator's "Naming / Backward Compatibility" lens
when env vars / log keys are renamed) but no structured Diataxis
coverage map exists.

**Files touched.**

- `prompts/review-consolidator.txt` (add an 8th lens: "Docs
  coverage (Diataxis)" — advisory).
- `agents.md` (document the new lens and its severity
  defaults).
- `README.md` (add env-var row: `REVIEW_DIATAXIS_LENS_ENABLED`).

**Implementation.**

1. Extend `prompts/review-consolidator.txt` with an 8th lens:
   *"**Docs coverage (Diataxis)** — advisory. For any PR with
   user-visible behaviour change (new env var, new workflow,
   new prompt phase, new schema, contract change), emit a
   `low`-severity finding listing the Diataxis categories that
   should be touched:*
   - *Reference (env-var tables, prompt-contract reference): does
     `README.md` env-var table or `agents.md` schema reference
     need an update?*
   - *How-to (operator runbooks): does
     `probably_unnecessary_but_read_if_stuck.md` need a
     runbook entry?*
   - *Tutorial (consumer-repo onboarding): does the
     workflow-template wrapper README need an update?*
   - *Explanation (`docs/plans/*-plan.md`, `agents.md` design
     rationale): does an explanation doc exist for this
     behaviour?*
   *Emit only the categories that genuinely need updates; do
   not enumerate untouched categories. If the PR already touches
   the relevant docs, emit `Docs coverage: complete`."*
2. The 8th lens is **advisory** (severity `low`) — it never
   gates merges. Adopting the existing §6 naming-immutability
   convention: the 7 existing lenses keep their byte-for-byte
   classification; the 8th is *added*, not renumbered.

**Flag + default.** `REVIEW_DIATAXIS_LENS_ENABLED` (default
`true`).

**Acceptance criteria.**

- Two consecutive feature-PRs produce a Diataxis lens finding;
  one accurately identifies a missing reference update.
- Zero blocker-severity findings emitted by the Diataxis lens
  (it must remain advisory).

---

## Phase N: CHANGELOG release-summary style guide

**Goal.** Add `docs/changelog-style.md` codifying the entry
structure: 1-2 sentence headline; lead paragraph (3-5 sentences,
what shipped and what changed for users); "The numbers that matter"
table when measurable; "What this means for [audience]" closing
paragraph. Voice rules: no AI vocabulary (delve, robust,
comprehensive, fundamental), no em-dashes, real numbers, real
filenames. Inspired by gstack's CHANGELOG voice rules but adapted
for our operator-facing entries.

**Status.** **Partial.** `CHANGELOG.md` follows Keep-a-Changelog
mechanically. Entries are dense and accurate but inconsistent in
voice — some are pure implementation detail; others mix audience
appropriately.

**Files touched.**

- `docs/changelog-style.md` *(new)*.
- `CLAUDE.md` (add §18 cross-reference pointing at the new
  style guide).
- `CHANGELOG.md` (no entry rewrites — going-forward only).

**Implementation.**

1. New `docs/changelog-style.md` covering:
   - Structure: headline / lead / numbers-table / audience-closing.
   - Voice rules: forbidden vocabulary (delve / robust /
     comprehensive / nuanced / fundamental / "Here's the
     kicker" / "The bottom line"), forbidden punctuation
     (em-dash where comma-or-period works), real numbers / real
     filenames mandatory.
   - Audience separation: end-user-facing changes in the lead;
     contributor-facing details in a "For contributors" subsection
     at the bottom.
   - Don't-do list: don't reference branch-internal version bumps;
     don't narrate the PR's revision history; don't post-hoc-
     rationalise scope decisions.
2. Add to `CLAUDE.md` a new section §18 "CHANGELOG style"
   (renumbering avoided per §6 — appended, not inserted) with a
   short cross-reference: *"All CHANGELOG entries follow
   `docs/changelog-style.md`. Voice rules are enforced at PR
   review time."*
3. Update `agents.md` to document the new file's existence and
   purpose.

**Flag + default.** No flag — style guide is a doc, not a runtime
behaviour.

**Acceptance criteria.**

- The next 3 CHANGELOG entries written after this lands follow
  the new structure (audited at PR review time).
- The style guide is referenced from `CLAUDE.md` and `agents.md`
  in a §6-compliant way (appended, not inserting).

---

## Phase O: Skill modularity refactor — shared prompt prelude

**Goal.** Extract the common prelude across `prompts/mode-*.txt`
(persona line, Output contract section, Q-ID format guidance,
BLOCKED: emission rules) into a single template file that all
mode prompts include at assembly time. Reduces drift (today, the
Q-ID format guidance lives in 3+ prompts and they have already
diverged slightly). Inspired by gstack's `scripts/resolvers/preamble.ts`
shared prelude. Pairs with `docs/plans/symphony-inspired-improvements-plan.md`
Phase S1 (strict prompt rendering).

**Status.** **Gap.** Each `prompts/mode-*.txt` is a flat file. Some
preludes have diverged.

**Files touched.**

- `prompts/_prelude_common.txt` *(new)*.
- `prompts/_prelude_clarify_and_plan.txt` *(new — Q-ID format)*.
- `prompts/_prelude_role_persona.txt` *(new — per-phase persona
  block, populated from Phase C)*.
- `scripts/assemble_prompt.sh` *(new — Jinja2-style strict
  template renderer)*.
- All `prompts/mode-*.txt` (replace the duplicated prelude with
  `{% include "_prelude_common.txt" %}` directives).
- `tests/test_assemble_prompt.py` *(new)*.
- `agents.md` (document the assembly step).

**Implementation.**

1. Extract the common prelude (everything before the per-phase
   role rules) from each `mode-*.txt` into
   `_prelude_common.txt`.
2. Q-ID format guidance (currently duplicated in
   `mode-clarify.txt` and `mode-plan.txt`) goes into
   `_prelude_clarify_and_plan.txt`.
3. The 16 persona blocks from Phase C live in
   `_prelude_role_persona.txt` (one block per mode-* prompt,
   keyed by basename).
4. `scripts/assemble_prompt.sh` renders the final prompt using
   strict template rendering (Jinja2 with `undefined=StrictUndefined`)
   — unknown variables and unknown filters fail rendering. Aligns
   with `docs/plans/symphony-inspired-improvements-plan.md` Phase S1.
5. CI gate: `tests/test_assemble_prompt.py` verifies every
   `mode-*.txt` renders to a byte-stable output across renders
   (golden-file pinning).
6. The final assembled prompts are written to
   `prompts/_assembled/mode-*.txt` at workflow setup time
   (existing `scripts/codex_setup_*` step), then codex-cli reads
   the assembled versions.

**Flag + default.** `PROMPT_PRELUDE_REFACTOR_ENABLED` (default
`false` during acceptance, flipped to `true` after byte-stable
golden-file verification on every mode prompt). When `false`,
codex-cli reads the original `mode-*.txt` directly (no
assembly).

**Acceptance criteria.**

- Every `mode-*.txt` renders to a byte-stable assembled output.
- The assembled outputs match the *current* prompt content
  byte-for-byte when persona blocks are empty (i.e., no
  behavioural change when Phase C is off).
- Zero regression on prompt-cache hit rate (verified via
  `INFO: openrouter usage … cache_read_input_tokens` log lines
  over 10 PRs).

---

## Phase P: AGENTS.md materiality finding in consolidator

**Goal.** When a PR makes structural changes (new env var, new
prompt phase, new log prefix, schema-touching, contract-touching)
AND `agents.md` is unchanged, the consolidator emits a
`high`-severity finding so the editor either updates
`agents.md` in the same PR or explicitly justifies leaving it.
Inspired by gstack's "AGENTS.md materiality reviewer" pattern.

**Status.** **Gap.** Today, doc drift is caught reactively (a
later PR or operator notices). No structural check forces
`agents.md` updates concurrent with changes.

**Files touched.**

- `prompts/review-consolidator.txt` (extend an existing lens —
  *not* adding a 9th lens to keep the 8-lens count stable; the
  Naming / Backward Compatibility lens already covers stable-log-
  prefix changes per `agents.md:130-147`, so we extend its prompt
  to include the materiality check).
- `agents.md` (document the new check).
- `README.md` (add env-var row: `REVIEW_MATERIALITY_CHECK_ENABLED`).

**Implementation.**

1. Extend the "Naming / Backward Compatibility" lens in
   `prompts/review-consolidator.txt` with:
   *"**Materiality of structural changes.** If the PR diff touches
   any of: (a) `scripts/*.sh` / `scripts/*.py` new function or
   new env-var reference; (b) `prompts/mode-*.txt` new phase or
   prompt-contract field; (c) `.github/workflows/*.yml` new job
   or new env block; (d) stable log prefix (see
   `agents.md:130-147`) emission; (e) memory-record schema, AND
   `agents.md` is unchanged in the same PR, emit a `high`-severity
   finding: 'AGENTS.md materiality: <one-line-summary-of-the-
   change> is operator-visible and `agents.md` is unchanged.
   Either update `agents.md` to document the new behaviour or
   justify in the PR description.' Treat as advisory if the
   change is documented in `README.md` or `docs/plans/*` — name
   the alternative documentation in the finding."*
2. Per §6 naming immutability, the existing seven lens names stay
   byte-for-byte stable; the materiality check is an *extension*
   of an existing lens, not a new lens.
3. The finding lands at `high` severity by default — actionable
   without being a merge blocker. Editor decides per CLAUDE.md
   §12.B / §12.D whether to update docs in-PR or defer.

**Flag + default.** `REVIEW_MATERIALITY_CHECK_ENABLED` (default
`true`).

**Acceptance criteria.**

- A test PR that adds a new env var without updating `agents.md`
  triggers the materiality finding.
- A test PR that adds an env var AND updates `agents.md` does
  not trigger the finding.
- A test PR that adds an env var AND documents it in
  `README.md` triggers the finding *advisorily* (the alternative
  doc is named in the finding).

---

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Phase A / B inflate plan-output token cost. | Both phases ship behind flags with fail-closed defaults during acceptance; cost is measured against current baseline (≤ 10 % drift acceptable). |
| Phase C persona prefixes destabilise prompt-cache hash. | Personas are appended as a fixed prefix block ahead of the existing role contract; cache-hash measurement on 10 PRs before flipping default to `true`. |
| Phase E plan-template additions encourage over-engineering small PRs. | Explicit guard in the prompt: "Do not pad a trivial PR with diagrams." Flag-gated; acceptance criteria includes "zero increase in median plan-output cost." |
| Phase F retro adds GitHub API surface. | One batched GraphQL query per week (well within §15 budget) plus one ai-memory paginated read. Cost ceiling documented. |
| Phase H lessons-learned schema drift. | Versioned schema (`lessons_learned_record.v1.json`); future schema changes go to `v2` per the existing schema-versioning convention. |
| Phase I tier resolver classifies a real bug PR as `lite` and misses a critical finding. | `[force-review]` PR title / `force-review` label override; acceptance criteria tracks tier mismatches via review-blocked-judge events; default-off during acceptance. |
| Phase J security audit produces noisy false positives. | Confidence-gate at ≥ 8/10; curated false-positive exclusion list; capped at 3 follow-up issues per week. |
| Phase K slop-scan linter-games the codebase. | Hard "what NOT to fix" guard set; consolidator filter prompt explicitly directs reviewer to reject linter-gaming findings. |
| Phase L scope-lock label blocks legitimate fixes that genuinely require touching adjacent files. | `BLOCKED: scope-lock-violation` emission gives the orchestrator a chance to dispatch clarification; operator can remove the label and re-trigger. |
| Phase M Diataxis lens generates noise on every code PR. | Advisory severity (`low`); explicit "emit only categories that genuinely need updates" guard. |
| Phase N CHANGELOG style guide is ignored. | Acceptance criteria includes audit of the next 3 entries at PR review time. |
| Phase O prompt-assembly refactor breaks an existing prompt. | Golden-file byte-stability test for every `mode-*.txt`; default-off during acceptance; assembly only runs when feature flag is on. |
| Phase P materiality finding has high false-positive rate. | The "documented in README.md or docs/plans/*" exception suppresses common false positives; advisory framing when alternative doc is present. |

## Rollout

Phases ship in cluster order (Cluster 1 → 2 → 3 → 4), with
parallel landings allowed *within* a cluster. Each phase ships
behind a flag with fail-closed (default-off OR default-on with
fail-open) configuration during the acceptance window. Flag
default flips happen one phase at a time, with a one-week
acceptance window per phase before the next flag flips.

**Cluster 1 — Prompt-shaping (phases A–E).**

1. Land Phases A and B together (both edit `mode-plan.txt`).
   `PLAN_SCOPE_MODE_REQUIRED=true`,
   `PLAN_REUSE_AUDIT_REQUIRED=true`. Acceptance: 5 consecutive
   plans verified; week 1.
2. Land Phase C (`PROMPT_PERSONA_PREFIX_ENABLED=true`).
   Acceptance: 3-PR A/B comparison; week 2.
3. Land Phase D (`DIAGNOSE_TRACE_REQUIRED=true`). Acceptance:
   5 diagnose runs; week 3.
4. Land Phase E (`PLAN_DIAGRAMS_OPTIONAL=true`). Acceptance:
   one diagram-bearing plan; week 4.

**Cluster 2 — Operational signal (phases F–I).**

5. Land Phase G (memory-hygiene CLI; no flag, opt-in by
   invocation). Week 5.
6. Land Phase H (`LESSONS_LEARNED_ENABLED=true`). Acceptance:
   ≥ 5 records after 14 days; weeks 6–7.
7. Land Phase F (`WORKFLOW_RETRO_ENABLED=false` first; flip to
   `true` after one successful weekly run). Weeks 6–8.
8. Land Phase I (`REVIEW_TIER_RESOLVER_ENABLED=false` first;
   flip to `true` after one week green dry-runs). Weeks 9–10.

**Cluster 3 — Quality gates (phases J–L).**

9. Land Phase L (`SCOPE_LOCK_LABEL_ENABLED=false` first; flip
   to `true` once one real issue exercises it). Week 11.
10. Land Phase K (`SLOP_SCAN_ENABLED=true`, fail-open). Week 12.
11. Land Phase J (`SECURITY_AUDIT_ENABLED=false` first; flip to
    `true` after one successful run). Weeks 13–14.

**Cluster 4 — Structure / discipline (phases M–P).**

12. Land Phase N (style guide; no flag). Week 15.
13. Land Phase M (`REVIEW_DIATAXIS_LENS_ENABLED=true`). Week 16.
14. Land Phase P (`REVIEW_MATERIALITY_CHECK_ENABLED=true`).
    Week 17.
15. Land Phase O (`PROMPT_PRELUDE_REFACTOR_ENABLED=false` first;
    flip to `true` after byte-stable golden-file verification).
    Weeks 18–19.

**Consumer-repo propagation (§14).** All flag defaults flip on
the workflow-source repo first. Consumer repos receive the new
behaviour via the existing `update_workflows.yml` daily cron +
`@stable` release `repository_dispatch`. Consumer-repo operators
can override any flag via repo vars without forking workflow
templates. No new propagation channel.

**Rollback.** Each phase's flag goes back to its fail-closed
default; the runtime reads the var on every workflow run, so
rollback is one repo-var edit away. For Phase O specifically
(prompt prelude refactor), rollback means deleting
`prompts/_assembled/*` and reverting the flag; the
`scripts/codex_setup_*` step will fall back to reading the
unmodified `mode-*.txt`.

## Open Questions

These survived the clarification batch and need resolution before
implementation starts.

- **OQ-1: Phase C — should the 7 consolidator lenses' classification
  names be left strictly byte-for-byte stable, or is a *prepend*
  of a persona block acceptable for consistency with the other 16
  mode prompts?** The Non-goals section forbids renaming the 7
  lenses; this question is about whether to prepend a persona
  block above each lens's existing classification name *inside
  `prompts/review-consolidator.txt`*. The conservative answer is
  no (don't touch the consolidator's classification text). The
  permissive answer is yes (prepend a persona above each lens for
  symmetry with mode-*.txt). My recommendation: **conservative —
  leave the consolidator unchanged in Phase C; the 7 lenses are
  already persona-shaped and renaming risks the log-prefix /
  ledger contract.**
- **OQ-2: Phase I — what is the trigger to flip the tier-resolver
  default from `false` to `true`?** Proposed: one week of green
  dry-runs (the resolver runs and emits its classification, but
  `review_run_reviewers.sh` ignores the result and runs the full
  panel anyway). After one week, audit the would-have-been tier
  vs the actual review outcome on 20+ PRs; flip only if there are
  zero would-have-been-`lite` PRs where a `full`-tier review
  found a high-severity finding. Reviewer confirmation requested.
- **OQ-3: Phase J — security audit cost ceiling.** The estimate
  is ~$5/week at `xhigh` reasoning over the default branch.
  Acceptance criteria caps per-audit at $10. Is that the right
  ceiling, or should we set a tighter $5 with `high` reasoning?
  Trade-off: `xhigh` catches more, `high` is half the cost. My
  recommendation: ship at `xhigh` for the first 4 weeks, then
  benchmark `high` against the same diff and flip to `high` if
  there's no meaningful finding regression.
- **OQ-4: Phase O — strict template engine choice.** Jinja2 is the
  Python ecosystem default and the natural fit; `docs/plans/symphony-inspired-improvements-plan.md`
  Phase S1 also picks Jinja2. Should the two phases share a
  single template engine implementation, or implement
  independently for shipping speed? My recommendation: **share —
  ship Phase S1 first if Symphony's plan lands first; otherwise
  Phase O lands the engine and Symphony's plan reuses it.**
  Coordination point with the sibling plan's author.
- **OQ-5: Phase F — retro posting channel.** GitHub Discussions
  vs `ai:retro` issue thread vs dedicated `.github/RETRO.md`
  weekly auto-commits. My recommendation: **`ai:retro` issue
  thread** — survives without GitHub Discussions feature
  enablement, leverages the existing tracking-issue pattern, and
  doesn't touch git history. Reviewer confirmation requested.
- **OQ-6: §14 propagation — should Phase F's retro be per-consumer-
  repo or workflow-source-only?** Per-consumer is more useful
  to operators but costs N × $0.05/week ≈ $0.55/week for 11
  consumers — small but non-zero. My recommendation:
  **workflow-source only in v1; add per-consumer in a follow-up
  plan only if v1 proves useful.**
- **OQ-7: Phase L scope-lock — should the post-commit
  verification fail-revert (current proposal) or hard-fail the
  workflow without revert (forcing operator inspection)?** My
  recommendation: **fail-revert** with a `[scope-lock-violation]`
  log line and a Telegram alert; cleaner than leaving a half-
  applied commit on the branch.
- **OQ-8: §6 — do any of the new env var names introduced here
  collide with existing or imminent names?** Quick audit (against
  `README.md` + `agents.md` env-var tables): `PLAN_SCOPE_MODE_REQUIRED`,
  `PLAN_REUSE_AUDIT_REQUIRED`, `PROMPT_PERSONA_PREFIX_ENABLED`,
  `DIAGNOSE_TRACE_REQUIRED`, `PLAN_DIAGRAMS_OPTIONAL`,
  `WORKFLOW_RETRO_*` (×4), `LESSONS_LEARNED_ENABLED`,
  `REVIEW_TIER_RESOLVER_ENABLED`, `REVIEW_TIER_LITE_*` (×2),
  `REVIEW_TIER_STANDARD_*` (×2), `SECURITY_AUDIT_*` (×4),
  `SLOP_SCAN_ENABLED`, `SCOPE_LOCK_LABEL_ENABLED`,
  `REVIEW_DIATAXIS_LENS_ENABLED`,
  `REVIEW_MATERIALITY_CHECK_ENABLED`,
  `PROMPT_PRELUDE_REFACTOR_ENABLED`. None collide on first read.
  Implementer to verify against the latest `README.md` table.

## References

- `garrytan/gstack` — README:
  <https://github.com/garrytan/gstack/blob/main/README.md>;
  CLAUDE.md:
  <https://github.com/garrytan/gstack/blob/main/CLAUDE.md>;
  ETHOS.md:
  <https://github.com/garrytan/gstack/blob/main/ETHOS.md>;
  AGENTS.md:
  <https://github.com/garrytan/gstack/blob/main/AGENTS.md>.
- `docs/completed/ai-code-review-learnings-plan.md` — sibling plan
  applying Cloudflare's `ai-code-review` lessons (overlaps
  Phase E here).
- `docs/completed/judge-loop-and-reissue-plan.md` (shipped) — covers
  judge-in-loop, sticky findings, typed rejections
  (overlaps Phase H here).
- `docs/plans/symphony-inspired-improvements-plan.md` — sibling plan
  applying Symphony's lessons (overlaps Phase O here on strict
  prompt rendering).
- `agents.md:130-147` — stable log prefixes (contractual under
  §6).
- `unattended_system_instructions.md` §2 ("Bias to Action"), §15
  ("Role-Specific Behavior"), §16 ("Output Contract").
- `CLAUDE.md` §0 (Prime Directive), §2 (Ask-First), §5 (Minimal
  Change Set), §6 (Naming Immutability), §10 (MongoDB), §12
  (PR Review Mode), §14 (Consumer Repo Registry), §15 (GitHub
  API Hygiene), §16 (Task Delegation).
- gstack's slop-scan reference: <https://github.com/benvinegar/slop-scan>.
- OWASP Top 10 reference: <https://owasp.org/Top10/>.
- STRIDE threat model reference (Microsoft):
  <https://learn.microsoft.com/en-us/azure/security/develop/threat-modeling-tool-threats>.
- Diataxis docs framework: <https://diataxis.fr/>.
