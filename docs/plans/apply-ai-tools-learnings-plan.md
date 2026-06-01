# Apply Learnings from External AI-Tools System Prompts

## Archival audit status (2026-06-01)

**BLOCKED:** archival for tracking issue #3024 remains open. This plan stays
canonical under `docs/plans/` because the re-audit against shipped HEAD
already finds the following confirmed load-bearing gaps:

1. **Goal 10 — naming verbosity.** `prompts/mode-implement.txt` says
   "Prefer descriptive names over abbreviations..." but still lacks the
   stricter "avoid 1–2 character names except in tight scopes" guidance this
   plan committed to land.
2. **Goal 12 — flow rubric.** `prompts/mode-implement.txt` keeps only a
   shortened "Preferred flow..." line and does not yet include the fuller
   discovery / status-update / batch-tool-call / reconcile cadence this plan
   specified.

Do not archive this plan into `docs/completed/` until those prompt gaps land
or a follow-up plan explicitly narrows the requirement.

## Summary

Adopt 13 additive prompt-improvement items distilled from the
`x1xhlol/system-prompts-and-models-of-ai-tools` repo (a public collection of
leaked / published system prompts from Cursor 2.0, Codex CLI Aug-2025,
Anthropic Claude Code 2.0, Amp gpt-5.yaml, VSCode-Agent gpt-5, Augment,
Warp, Windsurf, and others) into `unattended_system_instructions.md`,
`CLAUDE.md`, `prompts/mode-implement.txt`, the implement-diagnose / repair
prompts, and `prompts/review-reviewer-checklist.txt`. Every change is
strictly additive — no renames, no behavioural reversals, no MongoDB
contract impact, no consumer-repo propagation needed.

## Context

The trigger is the user's question "are there any learnings or improvements
in https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools that we
can apply to this repo?" A research subagent sampled 27 prompts from that
repo and produced four tiers of candidate improvements: quick wins,
high-leverage learnings, structural refactors, and contentious items. The
user elected to adopt the first two tiers as a concrete implementation plan
(this file) and to surface the remaining two tiers as a separate
future-considerations doc (`docs/ai-tools-future-improvements.md`, shipped
in the same PR).

The closest precedent in this repo is `docs/symphony-inspired-improvements.md`
— a similarly-structured "borrow mechanisms from an external system, drop
the parts that conflict with our values" plan. This plan follows the same
template: each adopted item names a source prompt, a target file in this
repo, and the proposed wording.

The two foundational system-instruction files for our pipeline are:

- `unattended_system_instructions.md` (373 lines, 19 sections) — the system
  context for **all** codex-cli unattended phases (clarify, plan,
  orchestrate, judge, implement, implement-repair, implement-diagnose,
  review autofix, conflict resolver, validate, workflow log analysis).
- `CLAUDE.md` (480 lines, 17 sections) — the system context for
  **interactive** Claude Code sessions only (this file is invisible to
  unattended pipelines).

Phase prompts under `prompts/mode-*.txt` and `prompts/review-*.txt` are
appended to `unattended_system_instructions.md` at run time; they hold the
phase-specific overrides.

CLAUDE.md sections that bind this work (cited verbatim where they constrain
the design):

- **§5 Minimal Change Set** — "Do NOT change formats, types, or unrelated
  logic. Extend existing mechanisms — never compete with them." Every
  addition below extends an existing section rather than introducing a new
  parallel rule.
- **§6 Backward Compatibility / Naming Immutability** — "NEVER rename,
  remove, or repurpose existing identifiers." No identifier is renamed by
  this plan; all section numbers are preserved.
- **§7 Output Requirements** — "List all files changed with line ranges of
  major logic changes." This plan does that in `## Files & Modules` below.

## Goals

Each goal is falsifiable by re-reading the target file after the
corresponding phase lands.

1. **Tool-name opacity** in user-visible artefacts — `apply_patch`,
   `multi_tool_use.parallel`, and similar internal tool names must not
   leak into PR comments, issue replies, or judge summaries. Internal
   logs are exempt. (Source: Cursor 2.0, Warp, Amp, Augment.)
2. **Anti-laziness** — forbid "you should…" / "consider…" / "we could…"
   in any phase whose deliverable is an artefact. The phase either produces
   the artefact or emits `BLOCKED:`. (Source: VSCode-Agent gpt-5, Cursor
   Agent 2.0.)
3. **Root-cause not symptom** — explicit rule against wrapping a real
   failure in a try / except / null-guard just to make a test pass, in
   `unattended_system_instructions.md` §15 role descriptions for
   Implementer, Editor, and Diagnoser. (Source: Codex CLI Aug-2025,
   Windsurf.)
4. **Status-update cadence** — a one-sentence preamble before each
   tool-call batch and a compact checkpoint after every 3–5 tool calls or
   after every >3-file edit burst. Materially reduces "4-minute silence"
   triage burden on `workflow-log-analysis`. (Source: Cursor 2.0
   `<status_update_spec>`, Codex CLI 20250820 "Preamble messages",
   VSCode-Agent gpt-5 "checkpoint every 3–5 tool calls".)
5. **Verification gates order** — typecheck → lint → tests → build →
   smoke. Stop at the first failing tier. (Source: Amp `gpt-5.yaml`.)
6. **No re-read after `apply_patch`** — the codex `apply_patch` tool
   errors on miss, so re-reading the file is wasted tokens. Tightens
   §4 without weakening shell-write verification. (Source: Codex CLI
   Aug-2025.)
7. **Subagent dos / don'ts** in `CLAUDE.md` §16 — "junior engineer who
   can't ask follow-ups" framing; explicit list of tasks belonging to a
   subagent vs. tasks that stay with the parent. (Source: Amp
   `gpt-5.yaml`, Anthropic Claude Code 2.0 Task tool.)
8. **Ambition vs. precision** — single sentence in §9 stating that the
   default is surgical (existing codebase) and scope only widens when the
   plan creates a new top-level subsystem with no incumbent code to
   respect. (Source: Codex CLI Aug-2025.)
9. **Plan-update-after-discovery** — when implement-time codebase truth
   diverges from the plan in a way that materially shifts scope, the
   implementer must note the divergence prominently in its output rather
   than silently absorbing it. (Source: Codex CLI 20250820 `update_plan`
   semantics, Windsurf `<planning>`.)
10. **Naming verbosity** in `prompts/mode-implement.txt` — descriptive
    function and variable names; ban one-letter names except in tight
    scopes. (Source: Cursor Agent 2.0 / 2025-09-03 `<code_style>`.)
11. **No inline comments unless requested** in `prompts/mode-implement.txt`
    — codifies what our top-level Claude Code system already does
    informally. (Source: Codex CLI Aug-2025.)
12. **`<flow>` rubric** in `prompts/mode-implement.txt` — single-sentence
    "discovery pass → todo / plan → status update → batch tool calls →
    reconcile → summary" cadence. (Source: Cursor Agent 2.0.)
13. **Reviewer fabrication ban** in `prompts/review-reviewer-checklist.txt`
    — every cited `file:line` must exist on the current ref and contain
    the buggy code. Citations to imagined lines are reviewer-fabrication
    and must be dropped before final output. (Aligns existing §15 Reviewer
    rule in `unattended_system_instructions.md` with the checklist.)

## Non-goals

- **No structural refactors.** XML-tag scaffolding across every
  `prompts/mode-*.txt` file (Cursor / Anthropic / Codex CLI style), the
  full `<preamble>/<checkpoint>/<final_summary>` three-block contract,
  reviewer `SEVERITY: BLOCKER|MAJOR|NIT` classification, and
  conflict-resolver intent-audit are deferred to
  `docs/ai-tools-future-improvements.md`. The lighter status-update
  cadence rule in goal #4 is the minimum viable subset of the larger
  structural refactor.
- **No contentious-items resolution.** Wholesale XML-ification (C1),
  private `<thinking>` blocks (C2 — conflicts with §17 forbidden
  behaviours), implement-time scope-creep gate (C3 — conflicts with
  `<completeness_contract>`), semantic-then-grep search-tool decision
  tree (C4), `AGENTS.md` industry-standard consolidation (C5), and
  Kiro-style requirements/design/tasks plan-phase split (S5) are
  enumerated in the future-considerations doc with pro / con and
  estimated cost; no work is proposed for them in this plan.
- **No code changes outside system instructions and phase prompts.** No
  workflow YAML edits, no script edits, no `scripts/codex_model_catalog.json`
  updates, no `agents.md` rewrites.
- **No model swaps, infrastructure changes, or env-var introductions.**
  Every addition lands as plain prose in an existing file.
- **No consumer-repo propagation.** The edited files are *not* under
  `workflow-templates/` and do not appear in
  `.github/ai/consumer_repos.json` dispatch payloads (per CLAUDE.md §14
  the registry governs workflow-template updates only). Consumer repos
  that mirror `prompts/*.txt` will pick up the changes the next time
  they sync via their own update path.
- **No new automated tests.** These edits add prose to system prompts;
  the existing prompt-rendering and YAML-syntax validators
  (`scripts/validate_changed_files_syntax.sh`) cover the only
  machine-checkable surface. Manual review of the next post-merge
  pipeline run is the validation.

## Constraints

This plan is bound by:

- **CLAUDE.md §5 Minimal Change Set** — every addition extends an
  existing numbered section. No new sections are created; no existing
  rule is rewritten.
- **CLAUDE.md §6 Naming Immutability** — no identifier (section number,
  log prefix, env var, file path, tool name, role label) is renamed.
  The §15 role labels (Reviewer / Aggregator / Consolidator / Editor /
  Implementer / Diagnoser / Judge) and the §16 `<verification_loop>`
  block name are preserved verbatim.
- **CLAUDE.md §10 MongoDB Rules** — N/A. No collection, query, or
  index work is touched by this plan.
- **CLAUDE.md §13 Repository Hygiene** — N/A. No writes into `.git/**`.
- **CLAUDE.md §14 Consumer Repo Registry** — N/A. The edited files
  (`unattended_system_instructions.md`, `CLAUDE.md`, `prompts/*.txt`)
  are not under `workflow-templates/` and do not require
  `repository_dispatch` propagation. The `.github/ai/consumer_repos.json`
  file is not edited.
- **CLAUDE.md §15 GitHub API Call Hygiene** — N/A. This plan adds no
  `gh api`, `gh_retry`, `_safe_gh_jq`, or `curl` calls.
- **`unattended_system_instructions.md` §17 Forbidden Behaviors** — the
  added rules must not conflict with the existing forbidden list
  (no interactive STOP-and-ASK; no mid-rollout pause; no claiming
  checks passed when not run). Goal #4 (status-update cadence) is
  consistent — the preamble is a one-sentence trace, not a wait state.
- **`unattended_system_instructions.md` §18 Intent Preservation** —
  goal #9 (plan-update-after-discovery) must preserve intent; the
  divergence note records reality without re-interpreting the issue.
- **Backward compatibility with downstream parsers.** The plan-phase
  Q-format parser
  (`Each option line MUST be exactly '- **A** — <description>
  (RECOMMENDED)'`, from `prompts/mode-plan.txt:77-79`), the orchestrator
  decomposition schema validator (`orchestrate_decomposition.v1`), and
  the review-bundle parser (`scripts/review_*.sh`) are all preserved
  byte-for-byte by this plan; no addition touches the canonical Q/A
  format or the structured JSON output of any phase.

## Approach

Group the 13 items into six small, thematically coherent commits. Each
phase touches a single file (Phase 6 touches two related prompts) so
review and rollback are surgical. No phase depends on another; if any
single phase regresses pipeline behaviour, that phase alone can be
reverted without disturbing the others.

**Why six commits, not one or thirteen?** Reviewer attention is the
binding constraint. One mega-commit obscures cause / effect for any
regression that surfaces after merge. Thirteen single-line commits
inflate the PR commit log without giving more rollback granularity than
the per-file grouping below. Per-file commits give us "revert one
commit, restore one file" rollback.

Alternative considered: ship each item as a separate PR. Rejected
because (a) the items share rationale and a single PR description gives
reviewers context; (b) the items are mutually independent so PR-to-PR
sequencing buys no real isolation; (c) one PR keeps the CHANGELOG and
the post-merge log-analysis runs tractable.

## Implementation Steps

### Phase 1 — Single-sentence additions to `unattended_system_instructions.md`

**Target file**: `unattended_system_instructions.md`
**Commit message**: `docs: add tool-name opacity, anti-laziness, root-cause, ambition-vs-precision, verification-order, and apply_patch-reread rules to unattended system instructions`

Six edits, listed in target-section order:

1. **§2 — anti-laziness sentence (goal #2).** Append to the final
   paragraph of §2 (after the "If a required input is a specific
   scalar value…" paragraph at line ~85 in current HEAD):

   > Anti-laziness: when the phase's deliverable is an artefact (file
   > edit, JSON object, comment text, report), produce the artefact
   > rather than emitting advice about it. Phrases of the shape "you
   > should…", "consider…", "we could…", or "this likely needs…" in
   > place of a concrete artefact are a failure mode equivalent to
   > stopping early. Either emit the artefact or emit `BLOCKED:` with
   > a scalar reason.

2. **§4 — `apply_patch` no-re-read sentence (goal #6).** Modify the
   existing bullet at line ~127–129 of `unattended_system_instructions.md`
   (`After ANY shell write, verify with git diff --stat …`) to scope it
   to shell writes only, and add a parallel bullet for `apply_patch`:

   > - After ANY shell write (heredoc, `printf`, redirected `cat`,
   >   `tee`), verify with `git diff --stat` scoped to the edited file.
   >   If zero lines changed, switch tools instead of retrying the same
   >   regex shape.
   > - After an `apply_patch` call, do not re-read the file to confirm
   >   the change landed — the tool raises on miss, so a successful
   >   return is sufficient evidence. Verify at end-of-rollout via
   >   `git diff --stat` on the full set of `apply_patch`-edited files.

   The existing bullet's literal text ("After ANY shell write…")
   becomes the first variant above; the parallel bullet for
   `apply_patch` is new. No identifier is renamed.

3. **§9 — ambition-vs-precision sentence (goal #8).** Append a new
   final bullet to §9 (Minimal Change Set, currently ending with
   "No opportunistic cleanup, unrelated refactors, or scope expansion."
   at line ~187):

   > - Default mode is **surgical**: existing-codebase edits make the
   >   minimum change the requirement allows. Only widen scope toward
   >   ambition when the plan explicitly creates a new top-level
   >   subsystem with no incumbent code to respect. Ambiguity defaults
   >   to surgical, never to ambitious.

4. **§15 — root-cause-not-symptom sentence (goal #3).** Append one
   sentence to each of three role descriptions: Editor (review autofix),
   Implementer (implement), Diagnoser / Judge. Wording (identical
   across the three placements for grep-ability):

   > Prefer root-cause fixes over symptom suppression. Wrapping a real
   > failure path in a try / except / null-guard to make a test pass or
   > silence a reviewer finding is forbidden unless the suppression is
   > itself the intended semantics.

   Placement: append to the last sentence of each role paragraph.

5. **§16 — tool-name opacity rule (goal #1).** Add a new bullet to the
   Output Contract list (currently four bullets at lines 318–322):

   > - When emitting user-visible artefacts (issue comments, PR bodies,
   >   judge summaries, plan reports), describe actions in natural
   >   language rather than tool names. Say "edited `path/to/file`",
   >   not "called `apply_patch` on `path/to/file`". Internal traces
   >   (codex stdout / stderr, `scripts/*.sh` logs) are exempt.

6. **§16 — verification-order extension (goal #5).** Add one bullet
   to the existing `<verification_loop>` block (inside the existing
   XML-style tag at lines 324–335):

   > - Executable-check order, when the phase wires up verification:
   >   typecheck → lint → tests → build → smoke. Stop at the first
   >   failing tier and address it before running later tiers. The
   >   cheapest signal first short-circuits expensive test runs.

   This addition stays inside the existing `<verification_loop>` tag;
   the tag name is preserved per §6 naming immutability.

**Estimated effort**: 30 minutes including diff review.

### Phase 2 — Status-update cadence subsection in `unattended_system_instructions.md` §3

**Target file**: `unattended_system_instructions.md`
**Commit message**: `docs: add status-update cadence subsection to unattended §3 tool-call discipline`

Add a new XML-tagged block at the end of §3 (after the existing bullet
list, before the `---` separator):

   > <status_update_cadence>
   > Emit one short preamble sentence (≤20 words) before each tool-call
   > batch that explains the immediate intent — e.g. "Reading the three
   > files the plan flags as touched." Run the tools in the same turn;
   > do not emit a preamble and then end the turn.
   >
   > After every 3–5 tool calls, OR after any burst that has produced
   > edits to >3 files since the last checkpoint, emit a compact
   > checkpoint of the form `Checkpoint: <bullet list of files touched,
   > what changed>`. Checkpoints are advisory traces, not summaries —
   > the §16 Output Contract summary still runs at end-of-rollout.
   >
   > Preambles and checkpoints go to the phase's stdout (the deliverable
   > stream the workflow log captures). They are not a substitute for
   > the §16 terminal summary and they do not count as the phase's
   > artefact.
   > </status_update_cadence>

The block name `<status_update_cadence>` is new — there is no existing
identifier with this name, so §6 does not apply (rename / removal
forbidden; new additions are allowed).

**Estimated effort**: 30 minutes.

### Phase 3 — `CLAUDE.md` additions

**Target file**: `CLAUDE.md`
**Commit message**: `docs: extend CLAUDE.md §7 with tool-name opacity and §16 with subagent dos/don'ts`

Two edits:

1. **§7 — tool-name opacity mirror.** Append a new bullet to the §7
   "In every final response:" list (currently two bullets at
   lines ~178–183):

   > - When describing what you did to the user, say "edited
   >   `path/to/file`" rather than "called `Edit` / `Write` /
   >   `apply_patch` on `path/to/file`". Internal logs and code
   >   comments are exempt — this rule only governs the chat reply
   >   text the user reads.

2. **§16 — subagent dos / don'ts.** Append a new "When to use a
   subagent" subsection after the existing "Spawn rules" list (currently
   ending at line ~463):

   > Use a subagent for:
   > - Feature scaffolding across multiple files where the task is
   >   well-specified and won't need follow-up clarification.
   > - Mass renames, bulk file generation, or repetitive lint sweeps.
   > - Parallel research (multiple independent reads / web fetches /
   >   grep sweeps) that would otherwise serialise on the parent's
   >   context window.
   >
   > Do NOT use a subagent for:
   > - Exploratory codebase mapping where the questions to ask
   >   emerge from intermediate answers.
   > - Architectural decisions or tradeoff weighing — those stay
   >   with the parent so the user's clarification answers are
   >   honoured.
   > - Debugging analysis where the fix may depend on follow-up
   >   questions to the user.
   >
   > Frame each subagent like a productive junior engineer who can't
   > ask follow-ups once started. If the task plausibly needs a
   > mid-rollout clarification, keep it in the parent.

**Estimated effort**: 20 minutes.

### Phase 4 — `prompts/mode-implement.txt` additions

**Target file**: `prompts/mode-implement.txt`
**Commit message**: `docs(prompts): add code-style, flow rubric, and plan-update-on-discovery rules to mode-implement`

Four additions, grouped under a new "Code-style hints" subsection added
between the existing "Dependency / lockfile discipline" block and
"Data-file output discipline" block (current line ~38 → ~40):

   > Code-style hints (for new identifiers introduced in this rollout
   > only; §6 of `unattended_system_instructions.md` still forbids
   > renaming existing identifiers):
   > - Prefer descriptive names. Functions are verb phrases
   >   (`computeFooScore`, not `getFoo` when the function does more than
   >   field access). Variables are noun phrases. Avoid 1–2 character
   >   names except in tight scopes (loop indices, lambda params,
   >   single-statement comprehensions).
   > - Do not add inline comments unless explicitly requested or the
   >   `why` is non-obvious from the code (a workaround for a specific
   >   bug, a subtle invariant, a hidden constraint). Comments that
   >   restate the code (`# increment counter`) are noise.
   > - Implementation flow: discovery pass (read the files the plan
   >   names) → confirm or note divergence (see Plan-divergence
   >   discipline below) → batch tool calls for edits → reconcile
   >   (verify with `git diff --stat`) → final summary per §16. Do not
   >   start editing before the discovery pass completes.

Then add a new "Plan-divergence discipline" subsection immediately
after the existing "Conflict detection" block (currently at lines
10–13):

   > Plan-divergence discipline: the existing Conflict-detection rule
   > above tells you to "proceed with the codebase truth and note the
   > conflict in your output." When the divergence is material — a file
   > the plan named does not exist, a function signature differs, a
   > dependency is at a different major version, a test the plan
   > references is gone — you MUST surface the divergence prominently
   > in your output rather than absorbing it silently. The output's
   > "Plan-divergence notes" line is read by the post-implement judge
   > and by `workflow-log-analysis`; silent absorption defeats both.
   > Continue with the safest interpretation per §2 of
   > `unattended_system_instructions.md`, encode the assumption as a
   > code comment, and record the divergence in the output.

**Estimated effort**: 30 minutes including a re-read of the resulting
file for clean flow with the existing sections.

### Phase 5 — Reviewer fabrication ban in `prompts/review-reviewer-checklist.txt`

**Target file**: `prompts/review-reviewer-checklist.txt`
**Commit message**: `docs(prompts): require reviewer to verify cited file:line exists on current ref`

Add one final paragraph after the existing format block (current line
22), before any trailing blank lines:

   > Fabrication ban: for every finding emitted under the seven lens
   > headings, the cited `File:` and `Line or code reference:` MUST be
   > a path that exists on the current ref and a line range that
   > contains the buggy code at review time. If you cannot read the
   > file or the cited lines do not match the described problem, drop
   > the finding before final output. Citations to imagined lines or
   > paths are reviewer-fabrication and are rejected by the
   > consolidator and the floor-rule promoter — they do not become
   > `FLOOR_MULTI_REVIEWER` even if multiple reviewers emit the same
   > fabricated cite.

The reference to `FLOOR_MULTI_REVIEWER` is to the existing log
prefix listed in `agents.md` — naming preserved per §6.

**Estimated effort**: 10 minutes.

### Phase 6 — Propagate plan-divergence discipline to implement-diagnose and implement-repair prompts

**Target files**:
- `prompts/mode-implement-diagnose.txt`
- `prompts/mode-implement-repair.txt`
- `prompts/mode-implement-repair-syntax.txt`

**Commit message**: `docs(prompts): propagate plan-divergence discipline to implement-diagnose and implement-repair prompts`

Add a one-paragraph cross-reference to each file (no full duplication;
each file just points at the canonical rule in `mode-implement.txt`):

   > Plan-divergence discipline: the canonical rule lives in
   > `prompts/mode-implement.txt`. If the diagnostic / repair you are
   > performing surfaces a divergence between the original plan and
   > the codebase truth, surface it in your output rather than absorbing
   > it silently. The post-implement judge reads "Plan-divergence
   > notes" lines from every implement-family rollout.

**Estimated effort**: 15 minutes including reading each file for the
right insertion point.

## Files & Modules

Files this plan creates, edits, or deletes. `[new]` = new file,
`[del]` = deletion, `[edit]` = additive edit in an existing file.
Line ranges below are present-tense (HEAD as of plan authoring); the
actual landing line ranges will shift as the file grows but the
target *section* is stable.

- `[new]` `docs/plans/apply-ai-tools-learnings-plan.md` — this file.
- `[new]` `docs/ai-tools-future-improvements.md` — companion analysis
  doc that enumerates the deferred structural refactors and
  contentious items (S1–S4, C1–C5, S5) for future consideration. Not
  an action plan; a backlog.
- `[edit]` `unattended_system_instructions.md` — six additive edits:
  - §2 (anti-laziness) — append one paragraph near current line 85.
  - §4 (apply_patch no-re-read) — modify existing bullet at lines
    ~127–129 to scope it to shell writes, add parallel bullet for
    `apply_patch`.
  - §9 (ambition vs precision) — append one bullet at current line
    ~187.
  - §15 (root-cause not symptom) — append one sentence to each of
    Editor (line ~300), Implementer (line ~305), Diagnoser / Judge
    (line ~310).
  - §16 (tool-name opacity) — add new bullet to the Output Contract
    list at lines 318–322.
  - §16 (verification order) — add new bullet inside the existing
    `<verification_loop>` tag at lines 324–335.
  - §3 (status-update cadence) — add new `<status_update_cadence>`
    XML-tagged block at end of §3, before the `---` separator.
- `[edit]` `CLAUDE.md` — two additive edits:
  - §7 (tool-name opacity mirror) — add new bullet to the "In every
    final response:" list at lines ~178–183.
  - §16 (subagent dos/don'ts) — append a new "When to use a
    subagent" subsection after current line 463.
- `[edit]` `prompts/mode-implement.txt` — two additive subsections:
  - New "Code-style hints" subsection between current line ~38 and
    line ~40.
  - New "Plan-divergence discipline" subsection after current lines
    10–13.
- `[edit]` `prompts/mode-implement-diagnose.txt` — one-paragraph
  cross-reference to the canonical plan-divergence rule.
- `[edit]` `prompts/mode-implement-repair.txt` — one-paragraph
  cross-reference.
- `[edit]` `prompts/mode-implement-repair-syntax.txt` — one-paragraph
  cross-reference.
- `[edit]` `prompts/review-reviewer-checklist.txt` — one new
  fabrication-ban paragraph appended after current line 22.

Total: 2 new files + 7 edited files. No deletions. No renames. No new
env vars, log prefixes, or identifiers (the new XML tag
`<status_update_cadence>` is the only new label, and it is internal to
the prompt body).

## Data Model / Index Changes

N/A. No MongoDB collection, query, index, or `/db/contracts/*` file
is touched by this plan. CLAUDE.md §10 does not apply.

## Tests

No new automated tests. Justification:

- **Static syntax**: every edited file is plain Markdown or plain text
  (no YAML, no JSON). `scripts/validate_changed_files_syntax.sh` runs
  on every implement-phase commit; markdown / text files are
  pass-through for that validator.
- **Prompt rendering**: phase prompts under `prompts/*.txt` are
  concatenated by `scripts/codex_prompt_assembler.*` (or the workflow's
  equivalent inline composer) into the codex-cli system prompt. The
  composer treats prompt files as opaque text — no template variables,
  no schema. Adding prose is safe.
- **Behavioural impact**: these additions ask the model to do strictly
  more (emit preambles, prefer root-cause, etc.). Regression risk is
  not a hard test failure; it is a soft quality drift surfaced by
  `workflow-log-analysis`. The check is the next post-merge
  pipeline run's report.

Manual verification after merge:

1. Trigger one smoke-mode `implement.yml` run on a tiny issue. Verify
   the codex transcript contains at least one preamble sentence and at
   least one checkpoint line for runs that make >3 file edits.
2. Trigger one `review_autofix.yml` run on an existing PR. Verify the
   reviewer bundle does not contain `apply_patch` / `multi_tool_use`
   tool-name references in the user-visible findings text.
3. Read the next `mode-workflow-analysis.txt` report. Confirm no new
   `AI_PHASE_FAILURE_V1` lines that reference the new wording (sanity
   check that the additions did not destabilise any phase).

## Risks & Mitigations

- **Risk**: the new status-update cadence (Phase 2) inflates
  workflow-log line counts and pushes the codex thread closer to the
  output-token cap. **Mitigation**: the cadence rule explicitly caps
  preambles at 20 words and checkpoints at compact bullet lists; the
  3–5 tool-call interval is the same gate Cursor 2.0 / VSCode-Agent
  ship with. Worst case revert: re-revert Phase 2 only.
- **Risk**: anti-laziness rule (Phase 1.1) causes the judge to emit
  `BLOCKED:` more frequently when previously it would have emitted
  "consider re-running the validator" guidance. **Mitigation**: §17
  forbidden behaviours already bans claiming-checks-passed, so the
  effective behaviour after this change is "produce verified artefact
  or BLOCKED" — the same artefact contract as the existing
  `<completeness_contract>` in `mode-implement.txt`. Existing judge
  prompts already produce structured JSON, not advice prose.
- **Risk**: root-cause rule (Phase 1.4) causes the autofix editor to
  refuse a legitimate symptom suppression (e.g. genuine null-default
  for a sometimes-absent optional field). **Mitigation**: the wording
  carves out "unless the suppression is itself the intended
  semantics." Editors that have a documented null-default for a
  field can satisfy this without changing behaviour.
- **Risk**: tool-name opacity rule (Phase 1.5 + Phase 3.1) requires
  the model to keep two vocabularies in mind (internal tool names for
  tool calls; natural-language verbs for user-visible artefacts).
  **Mitigation**: every model we run (gpt-5.4, claude-4.x, third-party
  reviewers) already supports this implicit translation; this rule
  codifies an existing preference rather than introducing a new
  capability requirement.
- **Risk**: plan-divergence discipline (Phase 4 + Phase 6) creates a
  new `Plan-divergence notes:` line in implement-phase outputs that
  downstream judge / workflow-log-analysis prompts do not yet read.
  **Mitigation**: the line is advisory; downstream prompts that don't
  recognise it ignore it (free-text section). A follow-up plan can
  teach the judge to read it explicitly if `workflow-log-analysis`
  reports the line is being emitted but ignored.
- **Risk**: reviewer fabrication ban (Phase 5) tightens reviewer
  output and may reduce raw-finding count on PRs where reviewers
  previously over-cited. **Mitigation**: the existing §15 Reviewer
  rule in `unattended_system_instructions.md` already says "Never
  fabricate file paths, line numbers, or quote spans." Phase 5
  operationalises that rule at the checklist layer; it does not
  introduce a new prohibition.
- **Risk**: backward incompatibility for consumer repos pinned to an
  earlier `prompts/*.txt` shape. **Mitigation**: consumer repos pin a
  release tag of `coding-workflows`, not individual prompt file SHAs.
  The next `@stable` tag picks up these additions; consumers stay on
  the prior tag until they opt in via their wrapper update.

## Rollout

- **Branch**: `claude/apply-ai-tools-learnings-v9sZZ` (assigned per
  session instructions). The default `claude/write-plan-<slug>` naming
  is overridden by the session-mandated branch.
- **Base**: `main` (resolved dynamically; do not hardcode).
- **PR**: opened as ready-for-review, not draft.
- **Commits**: six commits, one per phase, in numerical order. Phase
  ordering is reviewer-friendly (largest single-file edit first) and
  rollback-friendly (any single phase revert leaves the others
  functional).
- **No feature flag.** Every edit lands in a static system-prompt
  file consumed at codex-cli prompt assembly time. There is no
  runtime kill-switch to add. The kill-switch *is* git revert.
- **Rollback path**: `git revert <commit-sha>` of any single phase
  removes that phase's changes without disturbing the others. The
  PR's commit boundaries are the rollback unit.
- **Propagation timing**: immediate take-up on next pipeline run after
  merge to `main`. No `@stable` tag dependency for our own runs.
  Consumer repos pin a release tag and pick up changes via their own
  workflow-update path; this plan does not touch
  `.github/ai/consumer_repos.json` or trigger `repository_dispatch`.
- **Acceptance window**: 7 days of post-merge `workflow-log-analysis`
  reports. If reports flag any new `AI_PHASE_FAILURE_V1` lines or any
  regression in `LABEL_REPAIR` / `AUTOFIX_*` log patterns, isolate the
  responsible phase and revert that commit only.

## Open Questions

None remaining for this plan. The four clarification questions
(Q1–Q4) were resolved in the planning conversation:

- Q1 → slug = `apply-ai-tools-learnings` (matches branch).
- Q2 → implementation plan with concrete edits.
- Q3 → adopt quick wins (QW1–QW11) + high-leverage (L1–L10); defer
  structural refactors (S1–S4) to the future doc.
- Q4 → contentious items (C1–C5, S5) deferred to the future doc.

The deferred items are enumerated in
`docs/ai-tools-future-improvements.md` (shipped in the same PR) with
pro / con, estimated cost, and the open decisions required before
committing. That doc has its own "Open decisions" section; do not
duplicate those here.

## References

- **External source**: <https://github.com/x1xhlol/system-prompts-and-models-of-ai-tools>
  — public collection of AI-tools system prompts (Cursor, Codex CLI,
  Anthropic Claude Code, Amp, Augment, Warp, Windsurf, Devin, Replit,
  Junie, Kiro, Traycer, VSCode-Agent, Cline, and others).
- **Per-source files cited in goals**:
  - `Open Source prompts/Codex CLI/openai-codex-cli-system-prompt-20250820.txt`
    — goals #2, #3, #6, #8, #9, #11 (preamble, root-cause,
    no-reread-after-apply_patch, ambition-vs-precision, update_plan,
    no inline comments).
  - `Cursor Prompts/Agent Prompt 2.0.txt` and
    `Cursor Prompts/Agent Prompt 2025-09-03.txt` — goals #1, #4, #10,
    #12 (tool-name opacity, status-update spec, code-verbosity,
    `<flow>` rubric).
  - `Amp/gpt-5.yaml` — goals #5, #7 (verification gates order,
    subagent task framing).
  - `Anthropic/Claude Code 2.0.txt` — goal #7 (Task tool semantics:
    stateless agent invocations).
  - `VSCode Agent/gpt-5.txt` — goal #2 (anti-laziness phrasing).
  - `Windsurf/Prompt Wave 11.txt` — goal #9 (planning-update
    semantics).
- **In-repo precedent**:
  - `docs/symphony-inspired-improvements.md` — structurally similar
    "borrow mechanisms from an external system, drop the parts that
    conflict with our values" plan; used as the template for this
    plan's `Cross-Cutting Goals` / `Cross-Cutting Non-Goals` framing.
  - `docs/completed/judge-loop-and-reissue-plan.md` (shipped) — phase-gated rollout
    template for fail-open, flag-defaulted changes (this plan does
    not gate on flags because the edits are pure prose, but the
    rollback-per-phase model is borrowed).
- **In-repo constraint sources**:
  - `CLAUDE.md` §§5, 6, 7, 10, 13, 14, 15, 16 — interactive session
    rules cited above.
  - `unattended_system_instructions.md` §§2, 3, 4, 9, 15, 16, 17, 18
    — unattended pipeline rules extended by this plan.
  - `agents.md` — "Stable log prefixes (contractual)" section; the
    `FLOOR_MULTI_REVIEWER` reference in Phase 5 cites this.
- **Companion analysis doc**: `docs/ai-tools-future-improvements.md`
  (shipped in same PR) — backlog of deferred items S1–S4, C1–C5, S5.
