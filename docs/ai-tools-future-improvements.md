# Future Improvements from External AI-Tools System Prompts

## Purpose

This is a **backlog / consideration doc**, not an action plan. It enumerates
the structural refactors and contentious items surfaced by the research
into `x1xhlol/system-prompts-and-models-of-ai-tools` that the companion
implementation plan (`docs/completed/apply-ai-tools-learnings-plan.md`)
deliberately did NOT adopt. Each item lists what it would do, the source
prompts, pro / con, estimated cost, the open decisions that have to be
made before a real implementation plan can be drafted, and any
identified conflicts with this repo's existing conventions (CLAUDE.md
§6 naming immutability, §10 MongoDB, §14 consumer repos, etc.).

The list is ordered roughly by structural cost (smallest first). Items
S1–S4 are "structural refactor" candidates; items C1–C5 and S5 are
"contentious" candidates flagged because they conflict with — or
materially reinterpret — an existing rule.

This doc is shipped alongside the implementation plan so reviewers can
see what was considered and consciously deferred. When a future PR
decides to adopt any of these items, the corresponding section here
should be moved to a new `docs/plans/<slug>-plan.md` with the open
decisions resolved.

---

## S1. Wholesale XML-tag scaffolding across all phase prompts

### What it would do

Refactor every `prompts/mode-*.txt`, `prompts/review-*.txt`, and
`prompts/integration-sync-*.txt` file into a deterministic skeleton of
named XML-style blocks:

```
<role>...</role>
<inputs>...</inputs>
<outputs>...</outputs>
<rules>...</rules>
<verification_loop>...</verification_loop>
<output_contract>...</output_contract>
<failure_modes>...</failure_modes>
```

`unattended_system_instructions.md` already uses two such blocks
(`<tool_persistence_rules>` in §1, `<verification_loop>` in §16);
this would generalise the convention to all 30 phase prompts. The
implementation plan adopts a single new XML block
(`<status_update_cadence>` in §3) as the minimum viable subset; this
item is the wholesale version.

### Source

- Anthropic Sonnet 4.5 system prompt: every major rule lives in a
  named XML block (`<artifacts_info>`, `<refusal_handling>`,
  `<honesty_and_uncertainty>`).
- Cursor 2.0 Agent prompts: `<tool_calling>`, `<code_style>`,
  `<status_update_spec>`, `<flow>`, `<citing_code>`,
  `<communication>`, `<summary_spec>`, `<completion_spec>`.
- Codex CLI Aug-2025: `<persistence>`, `<tool_preambles>`,
  `<edit_tool_discipline>` — partial XML scaffolding mixed with
  Markdown headings.

### Pro

- **Discoverability**: a downstream consumer-repo override can
  monkey-patch one block (`<status_update_cadence>`) without
  copy-pasting the whole prompt file.
- **Grep-friendly**: `grep -l '<verification_loop>' prompts/` lists
  every phase that needs that block.
- **Structural consistency**: makes drift between phase prompts
  visible — if `mode-validate-generate.txt` is missing a
  `<verification_loop>` while `mode-implement.txt` has one, the
  asymmetry is obvious.
- **Composability**: easier to programmatically assemble phase
  prompts from named-block libraries (a future possibility — not
  required for this refactor).

### Con

- **One-time cost is ~2–4 hours** for the 30 prompt files. Reviewer
  attention cost is larger.
- **Wedge risk**: tag names become contracts. Once consumer repos
  start overriding `<status_update_cadence>`, renaming it (per §6
  naming immutability) becomes a breaking change. The first wholesale
  pass needs to commit to a stable tag vocabulary.
- **Diminishing returns**: most phase prompts are already short
  enough (28–276 lines) that adding XML structure costs more in
  visual noise than it buys in discoverability.

### Estimated cost

- 2–4 hours of focused editing.
- 1 hour of reviewer time for the resulting PR.
- 0 minutes of pipeline regression (pure prose / structural; no
  behavioural change).

### Open decisions before this becomes a real plan

- **One-shot vs incremental**: rewrite all 30 prompts in one PR, or
  XML-ify each prompt the next time someone edits it (organic
  migration)? Wholesale is finite-cost but big-PR-review-burden;
  organic is no-cost-day-one but unbounded drift window.
- **Tag vocabulary commitment**: which seven (or N) tag names become
  canonical? Borrow Cursor's vocabulary (`<role>`, `<rules>`,
  `<flow>`, `<status_update_spec>`, `<output_contract>`,
  `<failure_modes>`, `<citing_code>`) or invent our own?
- **§6 naming-immutability scope**: do the canonical tag names join
  the protected-identifier list in CLAUDE.md §6? If yes, rename of a
  tag name becomes a breaking change requiring aliasing.

### Known conflicts with existing conventions

- **§6 naming immutability**: once committed, tag names are
  identifiers and cannot be renamed without an alias migration.
- **Indentation policy (CLAUDE.md §9 / unattended §11)**: XML tags
  inside `.txt` files are not subject to YAML's tab-prohibition. No
  conflict.

---

## S2. Phase-specific preamble + checkpoint + final-summary contract

### What it would do

Promote the lightweight `<status_update_cadence>` block adopted in the
implementation plan (Phase 2) to a full three-block contract baked
into `unattended_system_instructions.md`:

```
<preamble>            # 1 sentence before each tool batch
<checkpoint>          # compact bullets every 3-5 tool calls or >3-file burst
<final_summary>       # 2-10 lines, lead with what changed and why, link files
```

Each block has a strict format spec (length, structure, allowed
content). Phase prompts then reference the block by name rather than
re-stating the rule.

### Source

- Cursor 2.0 Agent prompt: `<status_update_spec>` + `<summary_spec>` +
  `<completion_spec>` — three distinct blocks with separate length
  and content rules.
- Codex CLI Aug-2025: "Preamble messages" + "Sharing progress
  updates" + "Final answer structure" — three sections with explicit
  word counts (8–12 words for preamble, etc.).
- Amp `gpt-5.yaml`: "Final Status Spec (strict): 2–10 lines. Lead
  with what changed and why."

### Pro

- **Workflow-log triage**: every phase emits the same three-block
  pattern, so `workflow-log-analysis` can grep for `<final_summary>`
  to find phase outcomes without phase-specific parsing.
- **Reviewer trust**: a fixed final-summary format makes drift
  between "what the rollout claimed" and "what `git diff --stat`
  shows" easier to catch.
- **Backward-compat with implementation plan**: the
  `<status_update_cadence>` from the plan is the `<preamble>` +
  `<checkpoint>` halves of this; extending to `<final_summary>` is
  additive.

### Con

- **Replaces existing §16 Output Contract bullets**: the current §16
  has a free-form bullet list and a `<verification_loop>` block. A
  strict three-block contract would either (a) live inside §16 as a
  sub-spec or (b) replace the bullet list. Either path changes how
  every phase prompt currently structures its terminal output.
- **Per-phase override burden**: phases that legitimately need a
  shorter summary (e.g. `mode-implement-repair-syntax.txt` produces
  a 1-line fix) would need explicit carve-outs.
- **Risk of brittle format checking**: if a downstream parser
  starts requiring exactly the three blocks, model output that omits
  a block (e.g. a 0-tool-call rollout legitimately has no preamble)
  becomes a parse failure.

### Estimated cost

- 1 hour for `unattended_system_instructions.md` rewrite of §16.
- 30 minutes per phase prompt for "see §16 three-block contract"
  cross-references × ~10 prompts = 5 hours.
- 1–2 hours of consumer-repo regression bake.
- Total: ~8 hours.

### Open decisions before this becomes a real plan

- **Replace vs augment §16**: do the three new blocks replace the
  current §16 bullet list, or live alongside it as a stricter
  sub-spec? Replacement is cleaner but a §6 concern (renaming the
  output-contract shape).
- **Strict format vs guidance**: is `<final_summary>` "2–10 lines,
  must start with…" a hard constraint (parser fails outside the
  range) or guidance (model follows it ~95% of the time)? Strict
  needs a downstream parser update; guidance keeps the change
  prose-only.
- **Phase-specific overrides**: how does
  `mode-implement-repair-syntax.txt` (1-line fix output) carve out
  from the 2–10 line rule?

### Known conflicts

- **CLAUDE.md §6 naming immutability**: the existing §16 Output
  Contract bullet list is referenced by external prompts and
  documentation. Replacing it is a documented breaking change with
  alias path required.
- **`<verification_loop>` tag**: must be preserved per §6 since the
  name is already in use.

---

## S3. Reviewer SEVERITY classification (BLOCKER / MAJOR / NIT)

### What it would do

Add a `SEVERITY:` field to the reviewer issue format in
`prompts/review-reviewer-checklist.txt` with three allowed values:
BLOCKER, MAJOR, NIT. The consolidator
(`prompts/review-consolidator.txt`) already has a `SEVERITY:` field
with `blocker | high | med | low` — but the reviewer-side format does
not require reviewers to classify, leaving the consolidator to infer
severity from the finding text.

This item would (a) require reviewers to classify on emission, (b)
align the reviewer-side and consolidator-side vocabularies, and (c)
let the consolidator downrank NITs under load (consumer-repo PRs
sometimes ship with 50+ findings, dominated by style nits).

### Source

- Cursor 2.0 implicit severity classification (the agent prioritises
  "must-fix" before "consider").
- Anthropic Claude Code 2.0 TodoWrite tool (separates `urgent` /
  `high` / `med` / `low` priority).
- This repo's own `prompts/review-consolidator.txt` line 63 — already
  has SEVERITY: but only on the consolidator side.

### Pro

- **Volume management**: high-finding-count PRs become triagable.
  The consolidator can drop all NITs when total findings > N.
- **Existing infra already supports this**: the consolidator side has
  the vocabulary; the reviewer side just doesn't require it. Closing
  the gap is mechanical.
- **Aligns with floor-rule system**: `agents.md` documents the
  ≥2-reviewer floor rule and `FLOOR_MULTI_REVIEWER` log prefix.
  Severity-aware floors would let "two reviewers flagged a NIT in
  the same place" stay advisory while "two reviewers flagged a
  BLOCKER" remains non-skippable.

### Con

- **Reviewer-model calibration drift**: minimax-m2.5, kimi-k2.5,
  deepseek-v4-pro, qwen3.6-plus, and grok-4.1-fast all calibrate
  severity differently. Without explicit definitions in the prompt
  ("BLOCKER = breaks the build; MAJOR = silent data corruption or
  security; NIT = style / readability"), one reviewer's BLOCKER is
  another's MAJOR.
- **Existing consolidator vocabulary mismatch**: the consolidator
  side uses `blocker | high | med | low`; this item proposes
  `BLOCKER | MAJOR | NIT`. Aligning the vocabularies is itself a
  decision — keep the consolidator's 4-tier or move both to a
  3-tier model.
- **Risk of "everything is a BLOCKER"**: without strict definitions,
  every reviewer model will up-rank to BLOCKER to ensure findings
  aren't dropped.

### Estimated cost

- 1 hour for `prompts/review-reviewer-checklist.txt` to add the
  classification rules and definitions.
- 1 hour for `prompts/review-consolidator.txt` to align vocabulary
  (or 30 min if we keep both vocabs and the consolidator maps
  reviewer 3-tier → consolidator 4-tier).
- 1 hour for `scripts/review_floor_rules.sh` and `review_*.sh` to
  read the new severity field.
- 2 hours of soak: bake on one consumer repo's review pipeline to
  see calibration drift on real PRs.
- Total: ~5 hours.

### Open decisions

- **Vocabulary alignment**: keep both 3-tier (reviewer) and 4-tier
  (consolidator), or unify on one? Unifying breaks §6 unless we
  preserve the old vocabulary as aliases.
- **Strict definitions**: do we ship one paragraph per severity tier
  with concrete examples (`BLOCKER = npm test fails; MAJOR =
  hardcoded credentials; NIT = inconsistent indentation`)?
- **Default severity**: when a reviewer emits a finding without a
  SEVERITY: line, what's the default? MAJOR is the safest pick
  (downrank-able but not silently dropped).
- **Consolidator drop-under-load policy**: at what finding count
  does the consolidator start dropping NITs? Fixed threshold or
  proportional?

### Known conflicts

- **§6 naming immutability** on the consolidator's existing
  `blocker | high | med | low` vocabulary. Renaming requires
  aliasing.
- **Floor-rule contract** in `agents.md` (lines 169–179) — the
  `≥2-reviewer floor rule` is documented as non-overridable at
  classification time. Severity-aware floors would need an explicit
  contract update.

---

## S4. Conflict-resolver intent-audit trail

### What it would do

Extend `prompts/conflict-resolver.txt` and
`prompts/integration-sync-conflict-resolver.txt` to require the
resolver to record, for each conflicted file, a one-line note of
"what HEAD's side intended" and "what the other side intended"
*before* committing the resolution. The resolver's output already has
a "Conflicts resolved" section; this item adds a "Side-intent audit"
sub-section.

CLAUDE.md §12.G already says "Prefer the resolution that preserves
both sides' intent over the resolution that drops one side; never
silently discard either side's changes." This item operationalises
that rule at the resolver prompt layer.

### Source

- Codex CLI Aug-2025 conflict-handling guidance.
- This repo's own CLAUDE.md §12.G (interactive PR review mode).

### Pro

- **Catches silent-discard regressions**: if the resolver's side-
  intent note doesn't match what the resolution actually does, the
  judge or the post-resolve diff check can catch it.
- **Auditability**: a downstream debug of "why did this merge drop
  the import?" gets a structured answer from the resolver's own
  trace.
- **Aligns with §18 Intent Preservation** in
  `unattended_system_instructions.md`.

### Con

- **Output token cost**: every conflicted file adds ~3 lines of
  prose to the resolver output. On a large multi-file conflict
  (10+ files), that's 30+ extra lines of resolver-output to parse
  downstream.
- **Risk of hallucinated intent**: the resolver might confidently
  describe intent that isn't what either side actually meant,
  especially on incompatible edits where the "intent" is genuinely
  unclear.

### Estimated cost

- 30 minutes per resolver prompt × 2 = 1 hour.
- 30 minutes for any downstream parser that reads resolver output.
- Total: ~1.5 hours.

### Open decisions

- **Required vs optional**: must the resolver emit the intent audit
  for every conflict, or only for "incompatible-edit" conflicts
  (cases where the resolver had to choose one side)?
- **Format**: free-form prose, or a structured `INTENT_HEAD: …` /
  `INTENT_OTHER: …` template the orchestrator can grep?
- **Hallucination guard**: do we require the audit to quote the
  conflict markers verbatim before paraphrasing intent?

### Known conflicts

- **`prompts/conflict-resolver.txt` "Final output must be plain
  text"** (line 53): the new structured audit must still be plain
  text (no JSON). No conflict if we use the prose template.
- **CLAUDE.md §12.G**: this item operationalises it; no conflict.

---

## C1. XML scaffolding — wholesale vs. incremental

### Decision required

The implementation plan adopts one new XML tag
(`<status_update_cadence>` in `unattended_system_instructions.md` §3).
Items S1 and S2 above would push the XML convention further. The
open decision is:

- **A**: One-shot rewrite of all 30 phase prompts into the canonical
  XML skeleton.
- **B**: Organic migration — XML-ify each prompt the next time it's
  edited for another reason.
- **C**: Status quo — adopt new XML blocks only when there's a
  specific value-add (the implementation plan's
  `<status_update_cadence>`). No backfill.

### Why this is contentious

Option A is high-cost-but-finite; option B is no-cost-day-one-but-
unbounded-drift; option C is conservative-but-leaves-fragmentation.
The status-update-cadence adoption from the implementation plan
implicitly endorses (C) for now; explicit choice deferred to a
future PR.

### Recommendation deferred

No recommendation — depends on whether we expect to keep adding
XML-style blocks in the next 6 months. If yes, A pays off. If no,
C is fine.

---

## C2. Private `<thinking>` / `<scratchpad>` blocks

### What it would propose

Add a private reasoning block to `prompts/mode-implement.txt` and
`prompts/mode-judge.txt` of the form `<thinking>...</thinking>` that
is not surfaced to the user but does enter the model's context as a
self-prompt. Devin AI has this as a first-class `<think>` command
with 10 explicit triggers (before git actions; before
exploration→editing transitions; before reporting completion; when
CI fails; when going in circles).

### Why this is contentious

- **§17 Forbidden Behaviors** in `unattended_system_instructions.md`
  forbids "claiming checks passed when not actually run." A
  `<thinking>` block by itself is harmless if marked private, but
  the operational risk is the model confusing its internal trace
  for a verified outcome.
- **Codex-cli already produces an internal reasoning trace**
  (the `model_reasoning_effort = "xhigh"` setting). Adding a
  user-prompt-level `<thinking>` block is partly redundant.
- **Token-cost**: every `<thinking>` block competes with the codex
  thread's output budget.

### Pro

- **Reduces announce-without-emit risk**: a structured
  `<thinking>` block before every major edit gives the model a
  scratchpad to "rehearse" the tool call. The 2026-05-07
  openai/codex#11151 regression (`agents.md` lines 89–96) was
  exactly this failure mode — emitting reasoning and exiting
  without a tool call. A `<thinking>` block followed by a
  required tool call could force the emit.
- **Structured trace data**: workflow-log-analysis could mine
  `<thinking>` content for failure-mode patterns.

### Con

- **Behavioural shift in production**: changes how every implement
  / judge run renders its output. Hard to roll back if it
  destabilises a downstream parser.
- **Conflicts with the implementation plan's anti-laziness rule**:
  if anti-laziness forbids "you should…" / "consider…" in
  artefacts, a `<thinking>` block of "I should consider whether…"
  is structurally similar.

### Open decisions

- **Scope**: implement-phase only, or judge / diagnose too?
- **Format**: free-form, or structured (`<thinking><discovery>…
  </discovery><edits>…</edits></thinking>`)?
- **Visibility**: stripped from user-visible artefacts but kept in
  workflow-log? Or fully internal (model self-prompt only)?
- **Required vs optional**: must every rollout emit a `<thinking>`
  block, or only when triggers fire?

### Known conflicts

- **§17 Forbidden Behaviors**: needs explicit carve-out language.
- **§16 Output Contract**: the `<thinking>` block is not an
  "Action taken", "Assumption made", or "Missing-context note" —
  needs a new §16 sub-category or an explicit exemption.

---

## C3. Implement-time scope-creep gate

### What it would propose

`prompts/mode-implement.txt` would gain a hard gate: if the
implementer's actual edit set exceeds the plan's
`files_touched` (or implicit file enumeration) by N files (Amp's
gpt-5.yaml uses ">3 files" as the trigger), halt and emit
`BLOCKED: scope-creep` instead of silently editing.

### Why this is contentious

Conflicts directly with `prompts/mode-implement.txt`'s
`<completeness_contract>` (lines 64–69): "Treat the task as
incomplete until every required file change in the plan is on disk
or explicitly recorded as `BLOCKED: <reason>`." If the plan
undercounted files (common in early scoping), the scope-creep gate
fires on a legitimate completion.

### Pro

- **Catches plan-drift early**: an implement run that wants to
  touch 12 files when the plan named 3 is almost certainly either
  (a) wrong, or (b) flagging a plan that needs revision.
- **Aligns with `orchestrate.txt`'s file-partitioning discipline**
  (`prompts/mode-orchestrate.txt` lines 76–101 — the orchestrator
  already populates `files_touched` and treats sibling-issue
  overlap as a conflict).

### Con

- **False positives on legitimate plan undercount**: the plan
  phase's `files_touched` is "strongly recommended" but not
  required, and is often a best-effort guess.
- **Brittle on lockfile updates**: a one-line dependency manifest
  edit drags in a lockfile regen — does that count as 1 or 2 files
  in the scope-creep check?

### Open decisions

- **Trigger**: file count? line count? both? a multiplier of plan's
  estimate?
- **Override**: is the gate hard-fail (BLOCKED) or soft-warn
  (Plan-divergence note)?
- **Interaction with implementation plan Phase 4 (plan-divergence
  discipline)**: the implementation plan adds a soft-warn version
  of this rule (record divergence in output); C3 would tighten it
  to hard-fail.

### Known conflicts

- **`<completeness_contract>` in `mode-implement.txt`**: the
  hard-fail variant directly contradicts the "treat task as
  incomplete until every required file change is on disk" rule.
- **`prompts/mode-plan.txt` Scope-too-large gate** already exists
  at plan time (line 42–48): a plan with >10 files emits
  `BLOCKED: scope-too-large`. Adding a second gate at implement
  time could double-fail legitimate work.

---

## C4. Semantic-then-grep search-tool decision tree

### What it would propose

Add a sequencing rule to `unattended_system_instructions.md` §3
(Tool-Call Discipline):

```
1. semantic search (codebase_search / Semble / Serena) — for
   conceptual queries.
2. exact grep (rg, grep -r) — for known symbols.
3. read_file — for inspecting a specific known path.
```

Augment, Amp, and Cursor 2.0 all sequence in this order.

### Why this is contentious

- **Per-phase variation**: some phases (judge, implement-diagnose)
  benefit from semantic-first; others (implement, conflict-resolver)
  bypass semantic and go directly to read.
- **MCP server availability**: Semble and Serena are MCP servers
  whose availability varies by run (per `agents.md` log prefixes
  `SEMBLE_QUERY` / `SERENA_QUERY` and their `*_FALLBACK`
  counterparts). A hard sequencing rule that mandates
  semantic-first regresses when Semble / Serena are down.

### Pro

- **Avoids "grep for X, miss every variant"** failure mode that
  exact-string-match has on natural-language queries.
- **Aligns with existing fall-open infra**: Semble / Serena
  failures already fall back to legacy grep paths.

### Con

- **Codex `apply_patch` workflow is grep-and-edit, not
  semantic-and-edit**: forcing semantic-first slows
  one-line-fix rollouts.
- **Token cost**: semantic queries return more context than grep
  hits.

### Open decisions

- **Mandatory vs guidance**: hard "use semantic first" rule, or
  soft "prefer semantic when the query is conceptual"?
- **Per-phase override**: do conflict-resolver and `mode-implement-
  repair-syntax.txt` explicitly bypass the rule?
- **Cache layer**: do we cache semantic-query results in the
  cycle-local cache pattern (`agents.md` `ACTIVE_WORKFLOW_ISSUES`
  / `STALL_MANAGED_LINKED_PR_CACHE` family)?

### Known conflicts

- **`agents.md` Repo-specific batching helpers** (lines 113–127)
  enumerate the canonical batched GraphQL paths but not the
  search-tool order. C4 would add a new section to this list.
- **§15 GitHub API Call Hygiene** (CLAUDE.md): doesn't apply —
  Semble / Serena are MCP, not GitHub API.

---

## C5. AGENTS.md industry-standard consolidation

### What it would propose

Replace the current three-file split (`CLAUDE.md` interactive +
`unattended_system_instructions.md` unattended + `agents.md`
repo-architecture facts) with a single `AGENTS.md` that all agents
read, regardless of harness. Amp, Codex CLI, several other tools
treat `AGENTS.md` as the single ground-truth file.

### Why this is contentious

- **The current split is deliberate**: `CLAUDE.md` has STOP-and-ASK
  rules that unattended pipelines MUST NOT see (line 7–10 of
  CLAUDE.md is the explicit carve-out). `unattended_system_instructions.md`
  has bias-to-action rules. Merging them requires conditional
  branches inside the single file ("if interactive, do X; if
  unattended, do Y"), which is exactly what the split avoids.
- **Consumer-repo onboarding is currently "drop three files"**;
  industry convention is "drop one `AGENTS.md`". The friction is
  real but small.

### Pro

- **Industry alignment**: consumer repos onboarding new tools
  (Codex, Amp, third-party agents) find `AGENTS.md` automatically.
- **Single source of truth**: removes "did the consumer also ship
  `unattended_system_instructions.md`?" failure mode.

### Con

- **Breaks the carve-out**: the unattended-only rules and the
  interactive-only rules currently never co-occur. Merging needs
  per-paragraph guards.
- **Token cost**: a merged file is ~850 lines; every agent reads
  the union even if it only needs half.
- **Migration cost is high**: every reference to "see
  `unattended_system_instructions.md` §N" (and there are dozens in
  prompts, scripts, README, agents.md) becomes a rewrite.

### Open decisions

- **Carve-out mechanism**: conditional sections (`## §X (interactive
  only)`) or completely interleaved with explicit harness checks?
- **Backward compatibility**: keep the existing three files as
  aliases / redirects pointing at the new `AGENTS.md`?
- **Consumer-repo migration**: when does the existing
  `unattended_system_instructions.md` get deleted from consumer
  repos that have copied it?

### Known conflicts

- **CLAUDE.md §6 naming immutability**: all three current file
  names are referenced in the workflows, scripts, prompts; rename /
  removal is a documented breaking change.
- **CLAUDE.md preface** (lines 7–10): "The unattended pipelines
  (codex-cli driven) read `unattended_system_instructions.md`
  instead and never see this file." This is the carve-out C5 would
  reorganise; the reorganisation is the contentious part.

---

## S5. Kiro-style requirements / design / tasks plan-phase split

### What it would propose

Refactor `prompts/mode-plan.txt` to emit three structured artefacts
instead of one:

1. `requirements.md` — explicit `WHEN ... THEN ... SHALL ...` style
   acceptance criteria (EARS format).
2. `design.md` — files / interfaces / data flows.
3. `tasks.md` — ordered checklist of implementation steps.

Each gates the next with user approval (the current plan phase is
single-step).

### Why this is contentious

The current `prompts/mode-plan.txt` produces a 7-section plan with
an `Implementation-time estimate` cap. Splitting that into three
artefacts with three approval gates conflicts with the unattended-
pipeline model (`unattended_system_instructions.md` §2 bias-to-
action; the rollout cannot wait for human approval mid-stream).

### Pro

- **Larger plans become tractable**: a plan that exceeds the
  60-minute estimate today emits `BLOCKED: scope-too-large`. Kiro's
  split lets the plan phase respond with "I need three
  sub-implementations" instead of blocking.
- **EARS format is grep-friendly**: `WHEN X THEN Y SHALL Z` matches
  give the orchestrator a structured acceptance contract.

### Con

- **Conflicts with bias-to-action**: Kiro requires explicit user
  approval between each phase — impossible in unattended rollouts.
- **Heavy for small plans**: most plans are <60 min and don't
  benefit from a three-artefact split.

### Open decisions

- **Interactive-only or unattended too?**: Kiro's approval gates
  are inherently interactive. An unattended adaptation would have
  to auto-approve, defeating the gating's purpose.
- **EARS adoption**: is `WHEN ... THEN ... SHALL ...` worth the
  prose-density cost on small plans?
- **Sub-issue emission**: the orchestrator phase already decomposes
  large projects into sub-issues. Is Kiro's split useful at the
  plan phase, or would it duplicate orchestrator work?

### Known conflicts

- **`prompts/mode-plan.txt` Scope-too-large gate** (line 42–48): a
  three-artefact split would let plans exceed 60 min legitimately;
  changes the contract.
- **`prompts/mode-orchestrate.txt` author-supplied decomposition**
  (lines 14–31): the orchestrator already honours
  enumerated-issue authorship; Kiro at the plan phase overlaps
  with this.

---

## Aggregate effort estimate (if all items adopted)

| Item | Estimated cost | Risk |
|---|---|---|
| S1 (XML wholesale)          | 2–4 hr  | Low (prose only)     |
| S2 (3-block contract)       | ~8 hr   | Medium (parser risk) |
| S3 (reviewer severity)      | ~5 hr   | Medium (calibration) |
| S4 (intent-audit)           | ~1.5 hr | Low                  |
| C1 (XML decision)           | 0 hr    | Decision-only        |
| C2 (`<thinking>` blocks)    | ~3 hr   | Medium (§17 conflict)|
| C3 (scope-creep gate)       | ~2 hr   | High (false positives) |
| C4 (search decision tree)   | ~2 hr   | Medium               |
| C5 (`AGENTS.md` merge)      | 1–2 days | High (carve-out)    |
| S5 (Kiro split)             | 1–2 days | High (bias-to-action) |

Total if everything ships: ~5 working days. The items are mutually
independent (no item depends on another); they can ship as separate
PRs in any order.

## What this doc is NOT

- **Not an action plan.** Adopting any item requires its own
  `docs/plans/<slug>-plan.md` with the open decisions resolved.
- **Not a commitment.** Items here may be evaluated and explicitly
  rejected in a future round; this doc just preserves the analysis.
- **Not a feature roadmap.** Priority ordering between items is
  deliberately not set — the next round of work decides.

## References

- **External source**: <https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools>
- **Companion plan**: `docs/completed/apply-ai-tools-learnings-plan.md`
  (the implementation plan for the items adopted now).
- **In-repo precedent**: `docs/plans/symphony-inspired-improvements-plan.md` —
  similar structure for "borrow from external system, drop the
  parts that conflict with our values" analysis.
