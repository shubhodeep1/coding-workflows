# Learn-Claude-Code Learnings — Adopting Agent-Harness Patterns into the Unattended Pipeline

## Summary

`shareAI-lab/learn-claude-code` is a 60 k-star Python educational
re-implementation of a Claude-Code-style agent harness organised as 12
progressive stages (agent loop → tool dispatch → todos → subagents →
skills → context compact → tasks → background → teams → protocols →
autonomous → worktrees). The artefacts are not drop-in `.claude/`
configs, but eight of its **architectural patterns** map cleanly onto
gaps and partial implementations in our unattended pipeline (`scripts/`,
`prompts/mode-*.txt`, `.github/workflows/*.yml`,
`unattended_system_instructions.md`, `agents.md`). This plan classifies
each pattern against the current code, then ships it as a flag-gated,
fail-open phase (Phases A–H) — defaulting `false` until a per-phase
bake-out flips the default. Every phase respects
`unattended_system_instructions.md` §8 (env-var defaults), §10 (naming
immutability — new identifiers added alongside existing ones, never
replacing them), and §14 (GitHub API hygiene — no new per-iteration
API calls).

## Context

### Source

- Repo: `https://github.com/shareAI-lab/learn-claude-code` (MIT,
  `main`, last pushed 2026-05-11, ~60 k stars).
- Tagline: *"Bash is all you need — A nano claude code-like agent
  harness, built from 0 to 1."*
- Tutorial structure: `agents/s01_*.py` through `agents/s12_*.py` plus
  a `s_full.py` capstone wiring s02–s11 together. Docs in
  `docs/{en,zh,ja}/sXX-*.md` mirror the same content trilingually.
- Companion `web/` is a Next.js teaching visualiser; the only
  borrowable shapes there are the JSON formats in
  `web/src/data/scenarios/*.json` (replayable step traces) and
  `web/src/data/annotations/*.json` (design decisions with rejected
  alternatives).
- Scope of borrowable surface: **conceptual / architectural**, not
  drop-in. The harness uses Python `TOOL_HANDLERS` dicts,
  `threading.Lock`, `subprocess`, and a filesystem-only control plane
  (`.tasks/`, `.team/`, `.worktrees/`, `.transcripts/`). Our pipeline
  is GitHub-Actions-driven, codex-cli-driven, and partially
  MongoDB-backed — patterns must translate, not copy.

### How this plan differs from prior "external-learnings" plans

This is the third "borrow patterns from an external source" plan in the
repo. The first two are the templates for shape and depth:

- `docs/plans/apply-ai-tools-learnings-plan.md` — distilled 13 additive
  prompt improvements from `x1xhlol/system-prompts-and-models-of-ai-tools`.
  Plan was prompt-prose-only (zero code paths touched). Used the
  "cross-cutting goals / cross-cutting non-goals" framing.
- `docs/completed/ai-code-review-learnings-plan.md` — mapped 10 Cloudflare
  AI-code-review techniques onto `review_autofix`, classified
  already-done / partial / gap, and proposed flag-gated phases. That
  plan **did** touch code paths and used the
  `<flag>_ENABLED` / fail-open / bake-out convention. **This plan
  follows that shape.**

The novel content here vs the prior two:

- **Source is a harness reimplementation, not a prompt library or a
  product blog post.** So the borrowable artefacts are
  control-plane / state-plane / observability mechanics rather than
  prompt phrasing.
- **8 of the 25 candidate patterns are in scope (Q4: A+B+C+D+E+F+G+H);
  17 are dropped** — those 17 were either already trivially present
  (per-tool safe_path, denylist), Python-harness-only (TOOL_HANDLERS
  dispatch dict, threading), or conflict with our naming/contract
  rules (s09/s10 persistent teammates, the agent-builder skill's
  "level 0 = bash-only ≈ 50 LOC" minimalism).
- **Surface restricted to the unattended pipeline (Q3: A).** No
  changes to `.claude/`, `CLAUDE.md`, `workflow-templates/`, or
  consumer-repo propagation under `.github/ai/consumer_repos.json`.
  That scope decision drops the otherwise natural workflow-templates
  + §19 propagation tail.

### Grounded current-state evidence

A read-only audit of the eight patterns against
`scripts/`, `prompts/`, `.github/workflows/`,
`unattended_system_instructions.md`, and `agents.md` produced this
classification (audit summary, full details inline in each phase):

| Phase | Pattern | Classification | Key evidence |
|---|---|---|---|
| A | Three-layer context compaction discipline (s06) | partial | `scripts/codex_model_catalog.json` declares `auto_compact_token_limit`; no `.transcripts/<ts>.json` archive; no protect-recent-file-reads rule in any prompt |
| B | Identity re-injection after compaction (s11) | gap | Per-phase identity exists in `prompts/header.txt:1` + `prompts/mode-*.txt:1`, but `scripts/render_prompt.sh` has no compaction-aware re-inject step |
| C | File-per-task JSON + cascading `blockedBy` unblock (s07) | partial | `scripts/orchestrate_state_v2.py` chunks one wave-state doc across issue comments; `scripts/orchestrate_poll_process.sh` reads `.depends_on[]` and `.reissue_depends_on[]`; no per-task `.tasks/<id>.json` files |
| D | Append-only `events.jsonl` event bus (s12) | partial | `agents.md:130-147` lists 11 contractual stable log prefixes; `scripts/ai_memory.py:49` emits `AI_MEMORY_TELEMETRY: <JSON>` to stderr; no unified `.events.jsonl` stream |
| E | Nag-reminder injection (s03) | gap | Grep across `prompts/` and `scripts/` returns zero matches for silent-round tracking or `<reminder>` injection |
| F | Worktree-per-task with `.worktrees/index.json` (s12) | partial | `scripts/orchestrate_poll_process.sh` uses ad-hoc `git worktree add --quiet` / `remove --force`; no registry file; worktrees are transient and single-use |
| G | Replayable scenario JSON shape (web/data/scenarios) | partial | `scripts/collect_workflow_logs.py:47` truncates excerpts at `LOG_EXCERPT_MAX_CHARS=4000`; emits `SCHEMA_VERSION = "workflow_log_collector.v2"`; no replayable `[{type, content}]` step trace |
| H | Decision-annotations with `alternatives` field (web/data/annotations) | partial | `docs/plans/complete-squad-improvements-plan.md:189-198` has prose "Alternatives considered"; no enforced schema; no linter |

## Goals

Each goal is falsifiable from the resulting PR + repo state:

- **G-A.** A documented, prompt-injected compaction-discipline contract
  in `unattended_system_instructions.md` and an opt-in transcript
  archive helper that, when `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true`,
  writes `.transcripts/<sanitized-run_id>-<sanitized-phase>-<ts>.json` before any
  context-budget compaction trigger documented in
  `scripts/codex_model_catalog.json:auto_compact_token_limit`.
  Recent file-read tool results are explicitly protected from
  pre-compaction truncation by a prompt rule.
- **G-B.** When `UNATTENDED_IDENTITY_REINJECT_ENABLED=true`, every
  prompt rendered by `scripts/render_prompt.sh` carries a
  `<identity-recall>` block under the main role line that the model
  is instructed to re-emit verbatim if it has just performed a
  compaction. Default `false` until the bake-out flips it. No edits
  to existing `Role:` lines.
- **G-C.** A flag-gated parallel persistence path
  (`ORCH_TASK_FILES_ENABLED`, default `false`) writes one
  `.tasks/<wave>/<issue>.json` file per managed issue at every
  orchestrator-state checkpoint, in addition to the existing chunked
  state-comment write (§10 — additive, not replacing). Cascading
  unblock helpers (`unblock_dependents(issue_id)`) live in
  `scripts/orchestrate_lib.py` and rewrite `depends_on[]` /
  `reissue_depends_on[]` atomically.
- **G-D.** A unified `emit_event` shell + Python helper writes
  one-line JSON records to `.events/run-<run_id>.jsonl` when
  `EVENTS_JSONL_ENABLED=true`. Every existing stable log prefix
  (`LABEL_REPAIR`, `AUTOFIX_PEER_CHECK`, etc.) is **mirrored** to the
  JSONL stream — original stderr text emissions remain unchanged
  (§10). Schema versioned as `events.v1.json`.
- **G-E.** When `UNATTENDED_NAG_REMINDER_ENABLED=true`, the
  long-running phases that wrap codex-cli (`scripts/review_apply_fixes.sh`,
  `scripts/review_run_reviewers.sh`, `scripts/orchestrate_poll_process.sh`'s
  internal codex callsites) inject a `<reminder>...</reminder>` block
  after `UNATTENDED_NAG_SILENT_ROUNDS` (default `3`) consecutive
  silent assistant turns. Default `false`.
- **G-F.** A registry file `.worktrees/index.json` is written by a
  new `scripts/worktree_registry.sh` helper. The current ad-hoc
  worktree-create / worktree-remove sites in
  `scripts/orchestrate_poll_process.sh` and
  `scripts/review_conflict_resolve.sh` register and deregister
  worktrees through the helper when `ORCH_WORKTREE_REGISTRY_ENABLED=true`.
  A garbage-collection helper (`scripts/worktree_gc.sh`) reaps stale
  entries older than `ORCH_WORKTREE_TTL_SECS` (default 3600).
- **G-G.** A new `scripts/render_scenario_trace.py` emits a
  replayable JSON `[{type, content, ts, run_id, phase}]` trace from
  collected logs, gated by `WORKFLOW_LOG_SCENARIO_TRACE_ENABLED`
  (default `false`). It writes to
  `.ai/workflow_traces/<run_id>.scenario.json` with schema
  `workflow_scenario_trace.v1.json`. The trace **augments** the
  existing `workflow_log_collector.v2` outputs (§10) — does not
  replace them.
- **G-H.** A standard decision-block convention is documented in
  `prompts/mode-plan.txt` and a non-blocking linter
  (`scripts/lint_plan_decisions.py`) checks `docs/plans/*.md` for the
  presence of a `## Decisions` section with at least one record. The
  linter emits warnings (advisory) and never fails a workflow.

## Non-goals

- **Interactive surface (`.claude/`, `CLAUDE.md`,
  `.claude/commands/`, hooks).** Q3: A locked the surface to the
  unattended pipeline.
- **Consumer-repo propagation.** No edits to `workflow-templates/`;
  no entries flowing through `.github/ai/consumer_repos.json` /
  unattended-§19. If a consumer wants any of these patterns, that's
  a follow-up plan after upstream bake-out.
- **Renames of existing identifiers.** Naming immutability per
  unattended-§10. `depends_on` stays `depends_on` (not renamed to
  `blockedBy`); `AI_MEMORY_TELEMETRY` keeps its prefix; every stable
  log prefix listed in `agents.md:130-147` is mirrored into the
  JSONL stream rather than replaced. The 11 prefixes in scope:
  `LABEL_REPAIR`, `LABEL_REPAIR_DIFF`, `AUTOFIX_PEER_CHECK`,
  `AUTOFIX_DISPATCH_SKIPPED`, `AUTOFIX_DISPATCH_ISSUED`,
  `AI_PHASE_FAILURE_V1`, `SEMBLE_QUERY`, `SEMBLE_FALLBACK`,
  `SERENA_QUERY`, `SERENA_FALLBACK`, `SERENA_PROBE`.
- **MongoDB contracts.** None of the 8 patterns touch a collection,
  index, or `/db/contracts/*.yml`. Unattended-§12 not engaged.
- **Persistent multi-agent teammates (s09 / s10).** Q4 did not
  select them. The JSONL-mailbox + shutdown/plan-approval FSM
  patterns are interesting but overkill for the current
  GHA-sequenced clarify→plan→implement→review topology. Deferred to
  a future plan if and when the pipeline goes truly parallel.
- **Skills surface (s05).** Claude Code's own SKILL.md format
  already covers this; the harness's flat-skills design is a step
  back from our existing layout. No changes here.
- **Replacing codex-cli's own auto-compaction.** Phase A documents
  and supplements; it does not implement a parallel compactor that
  would compete with codex-cli's built-in behaviour (§9 — extend
  existing mechanisms, never compete).
- **Adopting the harness's permissive bash denylist** (`rm -rf`,
  `sudo`, `shutdown`, `/dev/`). Our existing sandbox is stricter; do
  not downgrade.
- **Translating the harness's `MODEL_ID` env-var convention.** The
  pipeline already uses per-phase `WORKFLOW_*_MODEL` repo-vars
  (README.md "Variables" table). No change.
- **Adopting the `init_agent.py` scaffolder.** Tagged as a future
  candidate (a `/init-workflow-template` scaffold) but outside this
  plan's scope.

## Constraints

These bind every phase and are cited inline below by section number:

- **`unattended_system_instructions.md` §8 (Env vars).** Every new env
  var introduced by this plan has an explicit default. Defaults are
  chosen such that flipping each `*_ENABLED=true` is the only change
  needed to opt in.
- **`unattended_system_instructions.md` §9 (Minimal change set).**
  Every phase **extends** the existing mechanism: chunked state, log
  prefixes, prompt frontmatter, etc. Never competes with codex-cli's
  internal compaction or with the existing
  `workflow_log_collector.v2` pipeline. The naming-immutability
  rule under §10 covers identifier preservation; §9 covers behaviour
  preservation when flags are off.
- **`unattended_system_instructions.md` §10 (Naming immutability).**
  No renames. New identifiers added alongside old. Specifically:
  `depends_on` (current key) stays the canonical field; the harness's
  `blockedBy` name is rejected. Existing stable log prefixes
  (`agents.md:130-147`) keep their text emissions; the JSONL stream
  is a mirror, not a replacement.
- **`unattended_system_instructions.md` §14 (GitHub API hygiene).**
  None of the phases add new `gh api` callsites. Phase G's scenario
  trace consumes the existing `collect_workflow_logs.py` output —
  it does not call GitHub itself. Phase D's `emit_event` is local
  filesystem only (no network).
- **`unattended_system_instructions.md` §11 (Code style).**
  Tab-indented shell + Python; YAML 2-space-indented. Opening braces
  on a new line.
- **`unattended_system_instructions.md` §13 (Repository hygiene).**
  Phase A's `.transcripts/`, Phase C's `.tasks/`, Phase D's
  `.events/`, Phase F's `.worktrees/`, Phase G's `.ai/workflow_traces/`
  are all under repo-root cache paths. None write into `.git/**`.
  All five paths are added to `.gitignore` (see Files & Modules).
- **`unattended_system_instructions.md` §16 (Output contract).**
  Phase B's identity re-injection block is purely additive — no
  existing `Role:` line changes. Phase E's nag reminder is a
  user-message injection that does not change the output schema of
  any phase. Phase H's decision schema is advisory; the linter
  warns but never errors.
- **`agents.md` "Stable log prefixes (contractual)" section.** Phase D
  expands this list with two new prefixes (`EVENTS_EMIT`,
  `EVENTS_EMIT_FAIL`) — additions to the contract, not changes.

## Approach

The eight phases divide cleanly along three dimensions:

| Dimension | Phases |
|---|---|
| Prompt-level disciplines (no script changes beyond rendering) | A, B, E |
| Filesystem state plane (new files, mirrored writes) | C, D, F, G |
| Documentation / lint conventions | H |

Cross-cutting design rules every phase honours:

1. **Flag default `false`.** Every behaviour change is opt-in via an
   `*_ENABLED` env var defaulting `false`. A per-phase bake-out PR
   flips the default after a clean production-log window.
2. **Fail-open.** Every phase's helper degrades to no-op + log line
   on any local I/O error. The host workflow continues. Mirrors the
   `AI_MEMORY_ENABLED` kill-switch precedent in
   `scripts/ai_memory.py` and the consolidator/ledger fail-open
   contracts in `agents.md`.
3. **Mirror, never replace.** Phase D mirrors stable log prefixes
   into JSONL — original stderr emissions remain. Phase C mirrors
   orchestrator state into per-task files — chunked-state comment
   write is unchanged. Phase G augments the existing log-collector
   output — does not replace it.
4. **Schema versioning.** Every new JSON shape carries a
   `schema_version` field. Phase D: `events.v1.json`. Phase G:
   `workflow_scenario_trace.v1.json`. Phase C: `task_state.v1.json`.
   Future schema bumps go through additive evolution.
5. **No new GitHub API calls** (§14). Every phase is local-only
   reads/writes against repo paths or in-process state.

Within each phase, the standard structure is:

- **Source.** One-line citation of the upstream s0X stage.
- **Goal.** The single sentence from "Goals" above.
- **Current state.** Evidence with `file:line` refs.
- **Approach.** Brief design summary.
- **Implementation steps.** Numbered, each step lists files touched
  and the change in one sentence.
- **Flag-gated rollout.** Env var name, default, bake-out plan,
  fail-open path.
- **Tests.** Unit + smoke coverage.
- **Risks & mitigations.** Bulleted.

---

## Phase A — Three-Layer Compaction Discipline (partial)

**Source.** `agents/s06_context_compact.py` + `docs/en/s06-*.md`.
Three layers: (1) micro-compact silently every turn (replace old
tool results with `"[Previous: used {tool}]"`; never compact
`read_file` results); (2) auto-compact when crude token estimate
(`len(str(messages)) / 4`) exceeds `auto_compact_token_limit`
(save full transcript to `.transcripts/<ts>.json`, ask model to
summarise, replace history); (3) manual `compact` tool.

**Goal.** G-A above.

**Current state.**

- `scripts/codex_model_catalog.json:24-30` declares
  `auto_compact_token_limit` per model. Codex-cli reads this and
  performs its own auto-compaction internally — the orchestrator
  has no visibility into the threshold being hit.
- `scripts/memory_helpers.sh:29` already emits
  `AI_MEMORY_TELEMETRY: <JSON>` lines that include `op: "compact"`
  for the memory subsystem — different concept (memory record
  compaction, not conversation context), but the telemetry pattern
  is reusable.
- No prompt currently instructs the model to protect recent
  `read_file` results from any future compaction step.
- No transcript archive directory exists; no helper writes to one.

**Approach.**

This pattern cannot fully replace codex-cli's built-in behaviour (§9).
What it **can** do upstream of codex-cli is:

1. Document the discipline as a contract in
   `unattended_system_instructions.md` so reviewers know what the
   model is expected to honour.
2. Add a prompt-level rule to every long-running phase
   (`mode-implement.txt`, `mode-review-*.txt` block in
   `prompts/header.txt`, the review-editor and conflict-resolver
   prompts) that explicitly says: *"If you compact context,
   preserve the most recent file-read tool result for each file you
   may need to re-edit."* This is a soft guardrail — the model
   honours it on a best-effort basis.
3. Add an opt-in transcript-archive helper
   (`scripts/transcript_archive.sh`) that, when
   `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true`, writes the codex-cli
   `--show-raw-conversation` output (or equivalent dump) to
   `.transcripts/<sanitized-run_id>-<sanitized-phase>-<ts>.json` before the phase
   exits. Provides a manual recovery escape hatch when an
   auto-compaction discards context that turns out to have mattered.

**Implementation steps.**

1. Add §20 to `unattended_system_instructions.md` titled
   "Context-compaction discipline" with three sub-clauses (a) never
   discard the most recent `read_file` / `read` tool result for any
   file the rollout may yet edit; (b) if the host harness signals
   that an auto-compaction is imminent, preserve the structured
   output contract (`Q<ID>` batch, JSON artefact, or `BLOCKED:`)
   verbatim in the summary; (c) when `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true`,
   trust that the host has archived the pre-compaction transcript
   to `.transcripts/<...>.json` — do not re-emit raw transcript
   data into the output artefact.
2. Patch `prompts/header.txt` to inline a `<compaction-rules>`
   block that restates §20.a in 2 lines. Header.txt is included in
   every phase prompt, so this guarantees coverage without touching
   each `mode-*.txt`.
3. Add `scripts/transcript_archive.sh` with one entrypoint
   `archive_transcript <run_id> <phase> <source_path>` that writes
   to `.transcripts/<sanitized-run_id>-<sanitized-phase>-<ts>.json` when
   `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true`. No-op otherwise.
   `set -euo pipefail`. Tab-indented per §11.
4. Wire the helper into the four codex-cli-driven phases that have
   visible per-call stdout/stderr capture: `mode-implement.txt` via
   `implement.yml`, `mode-judge.txt` via `orchestrate_poll.yml`,
   `review_apply_fixes.sh`, and `validate.yml`. Hook is exactly one
   line each, after the codex-cli invocation succeeds.
5. Add `.transcripts/` to `.gitignore`. Provide a runbook entry in
   `probably_unnecessary_but_read_if_stuck.md` describing how to
   replay a transcript by hand.

**Flag-gated rollout.**

- **Flag.** `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED` (default `false`).
- **Bake-out.** Land the prompt-rules + helper with the flag off.
  Run for one production week and confirm zero behavioural drift.
  Flip default to `true` in a follow-up PR titled
  `chore: enable UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED by default`.
- **Fail-open.** `archive_transcript` swallows any I/O error,
  emits `TRANSCRIPT_ARCHIVE_FAIL: <reason>` to stderr, and returns
  0. No phase ever fails because the archive failed.
- **Killswitch.** Setting the flag to `false` reverts to current
  behaviour; the prompt-rule section in `header.txt` is
  no-op-safe under the flag (the rule simply describes what to do
  when the host signals compaction, and codex-cli currently
  doesn't expose that signal, so the rule is dormant by default).

**Tests.**

- Shell smoke: `tests/test_transcript_archive.sh` — invoke the
  helper with the flag on/off, assert file presence/absence under
  `.transcripts/`.
- Prompt-render smoke: `tests/test_header_render.sh` — render
  `header.txt` and assert the `<compaction-rules>` block appears
  verbatim.
- No model-call test — the prompt rule is a soft guardrail and
  cannot be deterministically verified without an LLM run.

**Risks.**

- **R-A1: Model ignores the rule.** Mitigation — accepted; this is
  a soft guardrail. The transcript archive provides a recovery
  path when the model does drop a needed file-read result.
- **R-A2: `.transcripts/` fills disk on long runs.**
  Mitigation — runbook documents `find .transcripts -mtime +14
  -delete` cron entry; per-file size capped at 50 MB via
  `head -c` in the helper.
- **R-A3: Prompt-rule contradicts a future codex-cli compaction
  contract.** Mitigation — the rule is opt-in (the host signals
  compaction; if it never does, the rule is dormant). Revisit when
  codex-cli exposes a compaction-pre-hook.

---

## Phase B — Identity Re-Injection After Compaction (gap)

**Source.** `agents/s11_autonomous_agents.py` —
`make_identity_block(name, role, team)` prepended after history
shrinks to ≤ 3 messages so the agent doesn't forget who it is
post-summarisation.

**Goal.** G-B above.

**Current state.**

- `prompts/header.txt:1` opens with
  `Role: AI pipeline phase agent. Goal: produce the artefact described below.`
- Every `prompts/mode-*.txt` opens with a phase-specific
  `Role: ... Goal: ...` line:
  - `mode-implement.txt:1` — `Role: implementation-phase coder. Goal: implement the approved plan...`
  - `mode-judge.txt:1` — `Role: judge. Goal: evaluate whether the project is progressing correctly...`
  - `mode-clarify.txt`, `mode-plan.txt`, `mode-validate-*.txt`,
    `mode-workflow-*.txt`, etc. — same shape.
- `scripts/render_prompt.sh:1-82` renders dynamic placeholders
  (`{{WORKFLOW_EDIT_RESTRICTION}}`, `{{SEMBLE_PREFETCH}}`,
  `{{SERENA_TOOL_HINTS}}`). It has no compaction-aware step that
  re-emits the identity block mid-conversation.
- No phase prompt instructs the model to re-state its role if a
  compaction has occurred.

**Approach.**

Identity re-injection is naturally a model-side prompt rule because
the harness (codex-cli) hides the compaction event from outer
scripts. The pragmatic adoption is:

1. Add a `<identity-recall>` block to every rendered prompt that
   restates the phase's role + mission in 2 lines, with an explicit
   instruction: *"If you have just performed a context compaction
   (your summary message replaced earlier turns), re-emit this
   block at the top of your next assistant message before any tool
   call."*
2. The block is rendered by `render_prompt.sh` from a small
   template `prompts/_identity_recall.txt` parametrised on the
   phase name. Keeps the per-mode prompts untouched.

**Implementation steps.**

1. Add `prompts/_identity_recall.txt` with the recall template:

       <identity-recall>
       Phase: {{PHASE_NAME}}.
       Role: {{PHASE_ROLE}}.
       Mission: {{PHASE_MISSION}}.
       Re-emit this block at the top of your next message if you
       have just performed a context compaction.
       </identity-recall>

   The three placeholders are sourced from each `mode-*.txt`'s
   first-line `Role: ... Goal: ...` declaration.
2. Extend `scripts/render_prompt.sh` to read the first line of the
   per-mode prompt, parse `Role:` and `Goal:`, and substitute
   `{{PHASE_ROLE}}` / `{{PHASE_MISSION}}` into
   `_identity_recall.txt`. Inject the resulting block immediately
   after the existing first paragraph of the rendered prompt when
   `UNATTENDED_IDENTITY_REINJECT_ENABLED=true`.
3. Add a small `unattended_system_instructions.md` §21 line:
   *"Identity recall: when the host renders an `<identity-recall>`
   block, treat it as part of your role contract. Re-emit
   verbatim if you have just compacted."*

**Flag-gated rollout.**

- **Flag.** `UNATTENDED_IDENTITY_REINJECT_ENABLED` (default `false`).
- **Bake-out.** Land flag off. Hand-test one rendered prompt with
  the flag on to verify the block appears and the `Role:` parse
  works for every `mode-*.txt`. Flip default to `true` in a
  follow-up PR.
- **Fail-open.** If `Role:` / `Goal:` parsing fails on a given
  mode, `render_prompt.sh` skips the recall block and emits
  `IDENTITY_REINJECT_PARSE_FAIL: <mode>` to stderr. The prompt
  still renders; the phase still runs.

**Tests.**

- `tests/test_identity_recall_render.sh` — for every
  `prompts/mode-*.txt`, render with the flag on and assert the
  `<identity-recall>` block appears with non-empty `{{PHASE_ROLE}}`
  and `{{PHASE_MISSION}}`.
- Negative test — for a malformed prompt missing `Role:` /
  `Goal:`, assert the helper falls open and logs
  `IDENTITY_REINJECT_PARSE_FAIL`.

**Risks.**

- **R-B1: Block bloats context.** ~80 tokens. Mitigation —
  negligible relative to phase prompts (typically 1500–4000
  tokens).
- **R-B2: Model ignores the re-emit instruction.** Mitigation —
  this is a soft guardrail; the recall block itself doesn't change
  semantics, only re-anchors role. Worst case is a no-op.
- **R-B3: `Role:` line parse breaks on future prompt edits.**
  Mitigation — fail-open + parse-fail telemetry. Any contributor
  who edits a mode prompt and breaks the parse sees the log line
  in their CI smoke run.

---

## Phase C — File-Per-Task State + Cascading Unblock (partial)

**Source.** `agents/s07_task_system.py` — one `.tasks/task_{id}.json`
per task, schema `{id, subject, description, status, blockedBy[],
owner}`; on `completed`, `_clear_dependency(id)` walks all task files
and removes `id` from their `blockedBy` lists.

**Goal.** G-C above.

**Current state.**

- `scripts/orchestrate_state_v2.py` (chunked-state persistence)
  serialises one orchestrator-wave state document into 1..N
  GitHub issue comments. Each comment is a chunk of the same JSON
  doc; on read, chunks are reassembled.
- `scripts/orchestrate_poll_process.sh` (~lines 10767-10829)
  reads `.depends_on[]` and `.reissue_depends_on[]` arrays per
  issue from the reassembled state, and merges blocker lists via
  `jq`.
- `scripts/orchestrate_lib.py` handles wave-state validation,
  including dependency tracking and wave progression.
- No file-per-task layout exists. No `.tasks/<id>.json`. The
  cascade-unblock logic is implicit in the poll loop's per-cycle
  re-scan, not a single named helper.

**Approach.**

Adopt the file-per-task layout as a **parallel mirror** of the
existing chunked-state persistence. Both writers run; readers
continue to use the chunked-state comment path. Once a bake-out
window confirms parity, a future plan could flip the read path —
that flip is out of scope here (§9 — extend, don't replace).

The harness uses `blockedBy`; our existing identifier is
`depends_on`. §10 forbids the rename. We keep `depends_on` and
do not introduce `blockedBy` at all.

**Implementation steps.**

1. Add `scripts/task_state.py` with:
   - `write_task(wave_id, issue_id, state_dict)` — writes
     `.tasks/<wave_id>/<issue_id>.json` atomically (write-tmp,
     rename). Embeds `schema_version: "task_state.v1.json"`.
   - `read_task(wave_id, issue_id)` — reads back; returns `None`
     if missing.
   - `unblock_dependents(wave_id, completed_issue_id)` — walks
     every `.tasks/<wave_id>/*.json`, removes `completed_issue_id`
     from each `depends_on[]` / `reissue_depends_on[]`, writes
     back atomically. Logs `TASK_STATE_UNBLOCK <wave> <completed>
     <count_unblocked>`.
   - All operations are no-ops when `ORCH_TASK_FILES_ENABLED!=true`.
2. Patch `scripts/orchestrate_poll_process.sh` at every site that
   currently calls `orchestrate_state_v2.write_state` to **also**
   call `task_state.write_task` per managed issue. Mirror writes;
   the chunked-state comment write is unchanged (§9, §10).
3. Patch `scripts/orchestrate_lib.py` at the existing
   "issue marked complete" callsite to also invoke
   `unblock_dependents`. Mirror only — the existing implicit
   cascade in the next poll cycle stays unchanged.
4. Add `.tasks/` to `.gitignore`.
5. Document the schema in `agents.md` under a new
   "Task-state files (`.tasks/<wave>/<issue>.json`)" sub-section,
   noting that it is **mirror-only** until a future cut-over.

**Flag-gated rollout.**

- **Flag.** `ORCH_TASK_FILES_ENABLED` (default `false`).
- **Bake-out.** Run for two production waves (≈10–20 issues each)
  with the flag on; diff `.tasks/<wave>/*.json` against the
  reassembled chunked-state doc per cycle. Expected match-rate:
  100%. Flip default to `true` in a follow-up PR.
- **Fail-open.** Atomic-write failure (disk full, permission)
  swallows the error, logs `TASK_STATE_WRITE_FAIL <issue>
  <reason>`, and the poll loop continues. The chunked-state
  comment write is the authoritative path; the mirror is best-effort.
- **Killswitch.** `ORCH_TASK_FILES_ENABLED=false` reverts to the
  pre-mirror behaviour. The `.tasks/` directory can be safely
  deleted between runs.

**Tests.**

- `tests/test_task_state.py` — write/read round-trip,
  atomic-rename behaviour, schema-version stamping.
- `tests/test_task_state_unblock.py` — set up 5 tasks where T1
  depends on T2 and T3, T4 on T5; mark T2 complete; assert T1's
  `depends_on` array shrinks to `[T3]` and T3/T4/T5 are
  untouched.
- Mirror-parity test — fixture chunked-state input, invoke both
  writers, assert reassembled chunked-state equals concatenated
  `.tasks/<wave>/*.json` records by `issue_id`.

**Risks.**

- **R-C1: Drift between the two stores.** Mitigation — the
  mirror-only contract means the chunked-state is the read path.
  Drift is detectable in the bake-out (parity test) and never
  reaches the orchestrator's decisions.
- **R-C2: Atomic write performance on slow filesystems.**
  Mitigation — write to `.tasks/<wave>/.<issue>.tmp` then rename;
  per-issue write is ~1 KB.
- **R-C3: Disk usage.** Mitigation — per-wave directories can be
  pruned via a future GC pass; current waves contain ≤ ~30 issues
  ≈ 30 KB per wave.

---

## Phase D — Append-Only `events.jsonl` Event Bus (partial)

**Source.** `agents/s12_worktree_task_isolation.py` —
`.worktrees/events.jsonl`: every lifecycle transition emits a JSON
line. Append-only. Agents can query recent events via a tool.

**Goal.** G-D above.

**Current state.**

- `agents.md:130-147` lists 11 contractual stable log prefixes:
  `LABEL_REPAIR`, `LABEL_REPAIR_DIFF`, `AUTOFIX_PEER_CHECK`,
  `AUTOFIX_DISPATCH_SKIPPED`, `AUTOFIX_DISPATCH_ISSUED`,
  `AI_PHASE_FAILURE_V1`, `SEMBLE_QUERY`, `SEMBLE_FALLBACK`,
  `SERENA_QUERY`, `SERENA_FALLBACK`, `SERENA_PROBE`. These are
  emitted as stderr text lines from various `scripts/*.sh` and
  `scripts/*.py` callsites.
- `scripts/ai_memory.py:49` emits
  `AI_MEMORY_TELEMETRY: <JSON>` lines to stderr — the closest
  existing JSON-per-line shape, but limited to memory ops.
- `scripts/orchestrate_lib.py` (~line 270) extracts
  `<!-- AI_PHASE_FAILURE_V1 ... -->` markers from issue comments
  — this is a comment-embedded JSON envelope, again limited
  scope.
- No unified `.events.jsonl` stream. The workflow-log analysis
  pipeline (`scripts/collect_workflow_logs.py`) greps text
  prefixes per `RETRY_MARKERS` (line ~45-63), not a JSON stream.

**Approach.**

Add a shared `emit_event` shim (one shell function + one Python
function) that, when `EVENTS_JSONL_ENABLED=true`, appends a single
JSON record per call to `.events/run-${GITHUB_RUN_ID:-local}.jsonl`.
The shim **mirrors** every existing stable log prefix — the
original stderr text emission stays unchanged (§10). Mechanically:
each existing prefix-emission callsite is wrapped to also call
`emit_event`. The text stream remains the source of truth for
existing log-analysis greps; the JSONL stream is the new
machine-readable mirror for future tooling.

**Implementation steps.**

1. Add `scripts/emit_event.sh` with one entrypoint:
   `emit_event <prefix> <key=value ...>`. When
   `EVENTS_JSONL_ENABLED=true` and the events dir is writable,
   write one JSONL record:

       {"schema_version":"events.v1.json","ts":"<rfc3339>","run_id":"<github_run_id|local>","phase":"<UNATTENDED_PHASE>","prefix":"<prefix>","fields":{...}}

   to `.events/run-<run_id>.jsonl` (append, `flock`-protected).
   Otherwise no-op. Always returns 0.
2. Add `scripts/emit_event.py` with the equivalent Python helper
   `emit_event(prefix, **fields)`. Same on-disk format.
3. Patch each existing stable-prefix callsite to call
   `emit_event` after the existing `echo`/`print` text line. The
   text emission stays first so that any tail-following pipeline
   sees it unchanged. Audit (from `agents.md:130-147`):
   - `scripts/gh_helpers.sh` — `AUTOFIX_PEER_CHECK`,
     `AUTOFIX_DISPATCH_SKIPPED`, `AUTOFIX_DISPATCH_ISSUED` (audit
     the file for the exact callsites; the agents.md list is the
     contract).
   - `scripts/orchestrate_poll_process.sh` — `LABEL_REPAIR`,
     `LABEL_REPAIR_DIFF`.
   - `scripts/validate_process.sh` — `AI_PHASE_FAILURE_V1`.
   - `scripts/semble_helpers.sh` — `SEMBLE_QUERY`,
     `SEMBLE_FALLBACK`.
   - `scripts/setup_serena.sh` + `scripts/serena_stats_emit.py`
     — `SERENA_QUERY`, `SERENA_FALLBACK`, `SERENA_PROBE`.
4. Add two new prefixes to `agents.md:130-147`:
   `EVENTS_EMIT` (one record per event emitted; used for
   self-instrumentation) and `EVENTS_EMIT_FAIL` (emitted when
   the helper's own append fails). Document as additive to the
   contract.
5. Add `.events/` to `.gitignore`.

**Flag-gated rollout.**

- **Flag.** `EVENTS_JSONL_ENABLED` (default `false`).
- **Bake-out.** Land helper + mirror call sites with the flag off.
  Verify the text prefixes still emit unchanged (regression-test
  the existing grep paths in `collect_workflow_logs.py`). Flip
  the default on in a follow-up PR after a clean week.
- **Fail-open.** `flock` contention or write failure swallows
  the error, emits `EVENTS_EMIT_FAIL: <reason>` to stderr, and
  returns 0. The text prefix has already emitted.

**Tests.**

- `tests/test_emit_event.sh` — assert a JSONL record is written
  with the flag on, no record with the flag off.
- `tests/test_emit_event_concurrent.sh` — spawn 5 background
  emitters; assert no JSONL line is truncated (record-level
  atomicity via `flock`).
- `tests/test_log_prefix_regressions.sh` — sample a few existing
  log-prefix callsites and assert the text emission still
  appears with `EVENTS_JSONL_ENABLED=true`.

**Risks.**

- **R-D1: `flock` performance under concurrent emitters.**
  Mitigation — events are write-once-per-decision-point;
  expected throughput is <100/run. `flock` contention
  negligible.
- **R-D2: Disk usage on long runs.** Mitigation — per-run JSONL
  file, ~1 KB/event, cleaned up by workflow-run TTL (the
  `.events/` cache is ephemeral).
- **R-D3: Drift between text and JSONL.** Mitigation — every
  emission goes through a single `emit_event` wrapper that does
  both. A future PR could move the text emission inside the
  wrapper as well, but per §10 the text prefixes are stable
  contracts; the wrapper just adds the JSONL mirror.

---

## Phase E — Nag-Reminder Injection (gap)

**Source.** `agents/s03_todo_write.py` — tracks
`rounds_since_todo`; after 3 silent rounds, injects
`<reminder>Update your todos.</reminder>` as a user message
before the next LLM call.

**Goal.** G-E above.

**Current state.**

- Grep across `prompts/` and `scripts/` for `reminder`, `nag`,
  `silent.round` returns zero matches. No turn-count is tracked
  in any codex-cli wrapper.
- The long-running codex-cli callsites
  (`scripts/review_apply_fixes.sh`,
  `scripts/review_run_reviewers.sh`, the orchestrate-poll-judge
  invocation in `scripts/orchestrate_poll_process.sh`,
  `scripts/validate_process.sh`'s codex callsite) iterate the
  model without any silence detection.

**Approach.**

The pragmatic adoption is at the wrapper level, not via
codex-cli internals. After each codex turn the wrapper inspects
the last assistant message; if it contains zero tool calls (or
no `apply_patch` invocation in editor phases) for
`UNATTENDED_NAG_SILENT_ROUNDS` consecutive turns, the wrapper
appends a `<reminder>` user message before the next iteration.

Only three phases qualify as long-running enough to benefit:
review-editor, review-reviewer, orchestrate-poll-judge. The
implement phase has its own turn-count cap (`MAX_AUTOFIX_ITERATIONS`
analog in `mode-implement-repair.txt`) and is excluded.

**Implementation steps.**

1. Add `scripts/nag_reminder.sh` with one entrypoint:
   `maybe_inject_nag <phase> <silent_round_counter> <reminder_text>`.
   When `UNATTENDED_NAG_REMINDER_ENABLED=true` and the counter
   exceeds `UNATTENDED_NAG_SILENT_ROUNDS`, prints the reminder
   text to stdout and resets the counter; otherwise prints
   nothing. Caller is responsible for appending the output to
   the next codex-cli invocation's prompt.
2. Wire into the three long-running phases:
   - `scripts/review_apply_fixes.sh` — after each editor turn,
     if no `apply_patch` block appeared in the assistant output,
     increment the silence counter and call `maybe_inject_nag`
     with `Re-check the WILL_FIX list and emit an apply_patch
     block.`
   - `scripts/review_run_reviewers.sh` — after each reviewer
     turn, if no finding was emitted, inject
     `Continue reviewing or emit "no findings" explicitly.`
   - `scripts/orchestrate_poll_process.sh` (judge callsite) —
     after each judge turn, if no JSON verdict appeared,
     inject `Emit your JSON verdict now.`
3. Add three reminder strings to a new
   `prompts/_nag_reminders.txt` keyed by phase. Wrappers source
   from this file rather than hard-coding strings.
4. Document the pattern in `unattended_system_instructions.md`
   §22 — *"When the host injects a `<reminder>` message,
   re-engage with the task; do not respond with meta-commentary
   about the reminder itself."*

**Flag-gated rollout.**

- **Flag.** `UNATTENDED_NAG_REMINDER_ENABLED` (default `false`).
- **Threshold.** `UNATTENDED_NAG_SILENT_ROUNDS` (default `3`,
  integer in `1..10`; invalid clamps to `3`).
- **Bake-out.** Land helper with flag off. Flip on for the
  review-editor phase first; observe one production week of
  autofix runs; confirm the nag rate is low (target < 5 % of
  rounds). Flip on for the other two phases.
- **Fail-open.** If the helper cannot read
  `prompts/_nag_reminders.txt`, it logs
  `NAG_REMINDER_LOAD_FAIL` and returns empty (no injection).

**Tests.**

- `tests/test_nag_reminder.sh` — counter < threshold → no
  injection; counter ≥ threshold → injection + counter reset.
- `tests/test_nag_reminder_phases.sh` — for each of the three
  wired phases, confirm the correct reminder string is sourced
  from `_nag_reminders.txt`.

**Risks.**

- **R-E1: Nag triggers on legitimately reasoning-heavy turns.**
  Mitigation — threshold default is 3 (matches harness default);
  operator override available.
- **R-E2: Reminder text confuses the model into meta-commentary.**
  Mitigation — §22 explicitly forbids meta-commentary on the
  reminder.
- **R-E3: Silence-counter logic mistakes a tool call for silence.**
  Mitigation — wrapper-side detection uses explicit
  `apply_patch` (editor) / `# finding:` (reviewer) /
  `{"verdict":` (judge) markers per phase, not generic
  "tool call".

---

## Phase F — Worktree-Per-Task with `.worktrees/index.json` (partial)

**Source.** `agents/s12_worktree_task_isolation.py` — control plane
(`.tasks/`) vs execution plane (`.worktrees/index.json` + dirs).
Same `task_id` joins them. `WorktreeManager` wraps
`git worktree add -b wt/{name} .worktrees/{name} <base_ref>`. Name
validation `[A-Za-z0-9._-]{1,40}`. `EventBus` emits
`worktree.create.{before,after}`, `worktree.remove`,
`worktree.task.complete`.

**Goal.** G-F above.

**Current state.**

- `scripts/orchestrate_poll_process.sh` uses ad-hoc
  `git worktree add --quiet` and
  `git worktree remove --force "${wt}" 2>/dev/null || rm -rf "${wt}"`
  for merge-conflict resolution and integration testing.
- `scripts/review_conflict_resolve.sh` has the same pattern.
- No registry. No name validation beyond what `git worktree`
  itself enforces. No GC for stale worktrees if a workflow run
  is killed mid-worktree.

**Approach.**

Introduce `scripts/worktree_registry.sh` with `register`,
`deregister`, `list`, and `gc` subcommands. Existing callsites
wrap `git worktree add`/`remove` with `register`/`deregister`
when `ORCH_WORKTREE_REGISTRY_ENABLED=true`. Behaviour with the
flag off is identical to current.

`.worktrees/index.json` schema (`schema_version:
"worktree_registry.v1.json"`):

    {
      "schema_version": "worktree_registry.v1.json",
      "entries": [
        {
          "name": "<sanitised-name>",
          "path": ".worktrees/<name>",
          "branch": "wt/<name>",
          "task_id": "<issue|pr|local>",
          "created_at": "<rfc3339>",
          "owner_phase": "orchestrate-poll|review-conflict-resolve|...",
          "owner_run_id": "<github_run_id|local>"
        }
      ]
    }

GC reaps entries older than `ORCH_WORKTREE_TTL_SECS` (default
3600 s) whose `owner_run_id` is not in `gh run list --status
in_progress`.

Phase F also integrates with Phase D (events.jsonl): every
register/deregister emits an event prefix
`WORKTREE_REGISTER` / `WORKTREE_DEREGISTER` / `WORKTREE_GC`. These
are net-new prefixes — added to `agents.md:130-147`.

**Implementation steps.**

1. Add `scripts/worktree_registry.sh` with subcommands:
   - `register <name> <path> <branch> <task_id> <owner_phase>` —
     append entry to `.worktrees/index.json` atomically
     (read-modify-rename, `flock`-protected). Emits
     `WORKTREE_REGISTER` event.
   - `deregister <name>` — remove entry. Emits
     `WORKTREE_DEREGISTER`.
   - `list [--task <id>] [--owner-phase <name>]` — emit entries
     as JSON.
   - `gc` — reap stale entries (see GC criteria above).
2. Add `scripts/worktree_gc.sh` standalone wrapper invoked by a
   new cron-like step in `internal-orchestrate-poll.yml` (the
   existing `*/5 * * * *` poll already has the right cadence;
   add one `worktree_gc.sh` call near the end of each poll
   tick). Note: this re-uses the existing cron — no new
   schedule, no new API surface (§14).
3. Patch the two existing worktree callsites
   (`scripts/orchestrate_poll_process.sh`,
   `scripts/review_conflict_resolve.sh`) to call
   `register` after `git worktree add` succeeds and
   `deregister` before `git worktree remove`. Both calls are
   no-ops when the flag is off.
4. Add name validation per `[A-Za-z0-9._-]{1,40}` regex
   (sanitisation step in `register`). On violation, log
   `WORKTREE_REGISTER_INVALID_NAME` and return non-zero; the
   caller decides whether to fall back to current ad-hoc
   behaviour. The existing worktree paths in
   `orchestrate_poll_process.sh` already use compatible names
   (issue/PR numbers + suffix); audit during implementation
   that no current caller hits the validator.
5. Add `.worktrees/` to `.gitignore` if not already (currently
   the directory is created and removed inline; the registry
   file at `.worktrees/index.json` is new and must be ignored).

**Flag-gated rollout.**

- **Flag.** `ORCH_WORKTREE_REGISTRY_ENABLED` (default `false`).
- **TTL.** `ORCH_WORKTREE_TTL_SECS` (default `3600`; integer in
  `300..86400`; invalid clamps to `3600`).
- **Bake-out.** Land helper + wired callsites with flag off.
  Manually trigger an orchestrator merge-conflict path to
  exercise the registry; verify entry appears and disappears
  correctly. Flip default on after one production week.
- **Fail-open.** Registry I/O failure swallows the error, emits
  `WORKTREE_REGISTER_FAIL` / `WORKTREE_DEREGISTER_FAIL`, and the
  callsite proceeds with the bare `git worktree` operation.

**Tests.**

- `tests/test_worktree_registry.sh` — register / list /
  deregister round-trip; concurrent register stress test under
  `flock`.
- `tests/test_worktree_gc.sh` — fixture stale entry; GC
  removes; fresh entry preserved.
- `tests/test_worktree_name_validation.sh` — invalid names
  rejected; valid names accepted.

**Risks.**

- **R-F1: GC reaps a worktree still in use by a parallel run.**
  Mitigation — GC criterion checks `gh run list --status
  in_progress` against `owner_run_id`. One `gh api` call per GC
  pass (§14 — single batched call, not per-entry).
- **R-F2: `flock` contention.** Same as Phase D — negligible
  throughput.
- **R-F3: Registry file corruption.** Mitigation — atomic
  read-modify-rename; on read failure, registry is rebuilt from
  `git worktree list --porcelain` (one-off recovery; logged via
  `WORKTREE_REGISTRY_REBUILD`).

---

## Phase G — Replayable Scenario JSON Trace (partial)

**Source.** `web/src/data/scenarios/sXX.json` — every step of an
agent run captured as one of `user_message | assistant_text |
tool_call | tool_result`, with optional `toolName` and
`annotation`. Effectively a replayable trace.

**Goal.** G-G above.

**Current state.**

- `scripts/collect_workflow_logs.py:47` truncates log excerpts at
  `LOG_EXCERPT_MAX_CHARS = 4000` and emits `SCHEMA_VERSION =
  "workflow_log_collector.v2"`.
- `scripts/analyze_workflow_logs.py:26` consumes the collector
  output and produces a markdown analysis report
  (`prompts/mode-workflow-analysis.txt` defines the report
  shape).
- The collector output is **run-level metrics + text excerpts**,
  not a step-by-step model conversation trace.

**Approach.**

Build a `scripts/render_scenario_trace.py` that runs **downstream
of** `collect_workflow_logs.py`. It consumes the collector's
`workflow_log_report.json`, parses the per-phase codex-cli
stdout/stderr captures from log excerpts, and emits a normalised
`workflow_scenario_trace.v1.json` per run with shape:

    {
      "schema_version": "workflow_scenario_trace.v1.json",
      "run_id": "<github_run_id>",
      "phase": "implement|judge|review-editor|...",
      "steps": [
        {"type": "user_message", "ts": "...", "content": "...", "tokens": <int|null>},
        {"type": "assistant_text", "ts": "...", "content": "...", "tokens": <int|null>},
        {"type": "tool_call", "ts": "...", "name": "apply_patch", "args": {...}},
        {"type": "tool_result", "ts": "...", "output": "...", "exit_status": <int|null>}
      ]
    }

The trace is **opt-in via `WORKFLOW_LOG_SCENARIO_TRACE_ENABLED`**.
When off (default), no trace is produced — the existing analyze
pipeline is unchanged. When on, the trace is written to
`.ai/workflow_traces/<run_id>.scenario.json` and a new prefix
`WORKFLOW_SCENARIO_TRACE_WRITTEN` is emitted (mirrored to
events.jsonl when Phase D is also enabled).

The trace becomes useful input for:

- **Replay testing** — feed an old run's `steps` into a new
  model and diff outputs.
- **Anomaly detection** — query traces for "tool_call without
  apply_patch in editor phase" patterns.
- **Documentation** — the trace shape matches the upstream
  `web/src/data/scenarios/*.json` shape, so it can be rendered
  in a teaching visualiser if desired.

**Implementation steps.**

1. Add `scripts/render_scenario_trace.py` with one entrypoint
   `render(run_id, collector_output_path, out_path)`. Parses
   codex-cli output blocks from the log excerpts using existing
   stable markers (`>>> [model]`, `<<< [model]`,
   `[tool_call]`, `[tool_result]` — audit the codex-cli output
   format in `scripts/render_prompt.sh` / model catalog
   integration and codify the markers as constants at the top
   of the file).
2. Add a new step at the end of
   `.github/workflows/workflow-log-analysis.yml` (after the
   existing analyze step) that runs the trace renderer when
   `WORKFLOW_LOG_SCENARIO_TRACE_ENABLED=true`. Conditional;
   no-op when off.
3. Add `.ai/workflow_traces/` to `.gitignore`.
4. Document the schema in `agents.md` under a new
   "Workflow scenario traces" sub-section.

**Flag-gated rollout.**

- **Flag.** `WORKFLOW_LOG_SCENARIO_TRACE_ENABLED` (default
  `false`).
- **Bake-out.** Land trace renderer with flag off. Manually
  trigger `workflow-log-analysis.yml` with the flag on against
  one production run; verify trace shape matches schema and
  byte size is reasonable (< 1 MB / run). Flip default on after
  one clean week.
- **Fail-open.** Parse failure on a given run swallows the
  error, emits `WORKFLOW_SCENARIO_TRACE_PARSE_FAIL: <run_id>`,
  and the analyze pipeline continues with no trace for that run.

**Tests.**

- `tests/test_render_scenario_trace.py` — fixture log excerpt
  → expected trace JSON. Cover all four step types.
- Negative test — malformed codex-cli output → fail-open with
  parse-fail log; analyze step does not error.
- Schema-version stamp test.

**Risks.**

- **R-G1: codex-cli output format drift.** Mitigation —
  parser markers centralised at the top of the file; a future
  format change updates one place. Renderer fails-open, so
  drift doesn't break the analyze pipeline.
- **R-G2: Trace contains sensitive content from PRs / issues.**
  Mitigation — `.ai/workflow_traces/` is gitignored; only the
  workflow runner sees it. No upload step is added in this
  plan.
- **R-G3: Disk usage.** Mitigation — per-run trace
  ~ 100 KB-1 MB; cached cleaned up by workflow-run TTL.

---

## Phase H — Decision Annotations with `alternatives` (partial)

**Source.** `web/src/data/annotations/sXX.json` — per-stage design
decisions in the form `{ id, title, description, alternatives,
zh:{title,description}, ja:{...} }`.

**Goal.** G-H above.

**Current state.**

- `docs/plans/complete-squad-improvements-plan.md:189-198` has an
  "Alternatives considered" prose section.
- `docs/completed/judge-loop-and-reissue-plan.md` (shipped) and
  `docs/completed/ai-code-review-learnings-plan.md` also have prose
  alternatives sections.
- `docs/plans/apply-ai-tools-learnings-plan.md` does not.
- There is no enforced schema. No linter. No standard heading
  level. No machine-parseable record.

**Approach.**

Adopt a lightweight convention rather than a heavy schema:
**every plan in `docs/plans/*.md` SHOULD have a `## Decisions`
section.** Each decision is a markdown sub-section with a stable
`### D<n> — <title>` heading and three required sub-bullets:
**Chosen**, **Alternatives considered**, **Why**. Optionally a
**Rejected because** sub-bullet per alternative.

Example:

    ## Decisions

    ### D1 — Mirror vs. replace orchestrator state

    - **Chosen:** mirror — write `.tasks/<wave>/<issue>.json` in
      addition to the existing chunked-state comment.
    - **Alternatives considered:**
      - **Replace** — drop the chunked-state comment path entirely
        and read from `.tasks/*.json`. *Rejected because:* breaks
        wave recovery if `.tasks/` is wiped between runs.
      - **Defer** — keep current chunked state, defer file-per-task
        to a future plan. *Rejected because:* future plan loses the
        flag-gated parity window.
    - **Why:** see §10 — additive, naming-immutable, reversible
      via flag.

The convention is enforced by a non-blocking linter
(`scripts/lint_plan_decisions.py`) that emits a warning per plan
file missing the section. Linter runs in a new optional
workflow step; never blocks merge.

**Implementation steps.**

1. Add `scripts/lint_plan_decisions.py` that walks
   `docs/plans/*.md`, parses sections, and emits a warning per
   file missing `## Decisions` or per `### D<n>` block missing
   one of the three required sub-bullets. Output is to stderr;
   exit 0 always.
2. Add an optional step to a relevant workflow (`ci.yml` or a
   new `lint-plans.yml`) that runs the linter on push. Step is
   `continue-on-error: true` and surfaces warnings in the run
   summary. Audit which workflow has the most-trafficked CI
   surface; add the linter there.
3. Document the convention in `prompts/mode-plan.txt` —
   instruct the plan-emitting model to include a `## Decisions`
   section per the schema. This influences future
   plan-generating runs; existing plans are not retro-edited
   here (a follow-up housekeeping PR can do that).
4. Add an example `## Decisions` block to **this plan**
   (`docs/plans/learn-claude-code-learnings-plan.md`, below) as
   the canonical reference.

**Flag-gated rollout.**

- **Flag.** `DOCS_DECISION_LINT_ENABLED` (default `false`). When
  off, the linter still runs but its non-zero warning emissions
  are suppressed. When on, warnings surface in CI logs.
- **Bake-out.** Land convention + linter with flag off. Validate
  the linter parses `apply-ai-tools-learnings-plan.md`,
  `docs/completed/ai-code-review-learnings-plan.md`,
  `complete-squad-improvements-plan.md`,
  `docs/completed/judge-loop-and-reissue-plan.md`, and this plan
  correctly.
  Flip flag on after one clean week.
- **Fail-open.** Linter is advisory only. Plan PRs can land
  without `## Decisions`; the linter just warns. (`continue-on-error`
  on the step.)

**Tests.**

- `tests/test_lint_plan_decisions.py` — fixture plans with /
  without the section; assert warning emission shape.
- Self-test — assert this plan and the four existing plans pass
  the linter (after retrofit, see Open Question OQ-H1).

**Risks.**

- **R-H1: Convention is ignored by future plan authors.**
  Mitigation — linter warning + `mode-plan.txt` rule. No enforcement.
- **R-H2: Existing plans don't follow the convention.**
  Mitigation — backfill is OQ-H1 (whether to retrofit existing
  plans, or accept them as legacy).

## Implementation Steps (sequenced across phases)

This plan implements 8 phases, all independent. Recommended landing
order favours small, observable wins early:

1. **Phase H** — docs-only, no behaviour change. Establishes the
   `## Decisions` convention used elsewhere.
2. **Phase D** — `emit_event` + mirrored prefixes. Enables the
   observability substrate that Phases F and G can mirror into.
3. **Phase B** — identity re-injection. One-file prompt template
   + `render_prompt.sh` patch. Cheapest behavioural win.
4. **Phase A** — compaction discipline. Mostly prose, plus the
   transcript-archive helper.
5. **Phase E** — nag-reminder. Three wired callsites; opt-in by
   phase.
6. **Phase C** — file-per-task mirror. Touches orchestrate-state
   paths; needs careful bake-out diff against chunked state.
7. **Phase F** — worktree registry. Touches two callsites and
   adds a GC pass to the existing poll cron.
8. **Phase G** — scenario trace. Downstream of
   `collect_workflow_logs.py`; the biggest renderer.

Each phase lands as its own PR. Per the "single-PR rule" in
interactive review mode (CLAUDE.md §12.A) — that rule binds
interactive sessions; unattended-rollout PRs are sequenced
independently and may land in parallel. Implementation can run all
8 in parallel if desired; bake-outs serialised per phase.

## Files & Modules

New files:

- `[new]` `scripts/transcript_archive.sh` — Phase A.
- `[new]` `scripts/task_state.py` — Phase C.
- `[new]` `scripts/emit_event.sh` — Phase D.
- `[new]` `scripts/emit_event.py` — Phase D.
- `[new]` `scripts/nag_reminder.sh` — Phase E.
- `[new]` `scripts/worktree_registry.sh` — Phase F.
- `[new]` `scripts/worktree_gc.sh` — Phase F.
- `[new]` `scripts/render_scenario_trace.py` — Phase G.
- `[new]` `scripts/lint_plan_decisions.py` — Phase H.
- `[new]` `prompts/_identity_recall.txt` — Phase B.
- `[new]` `prompts/_nag_reminders.txt` — Phase E.
- `[new]` `tests/test_transcript_archive.sh` — Phase A.
- `[new]` `tests/test_header_render.sh` — Phase A.
- `[new]` `tests/test_identity_recall_render.sh` — Phase B.
- `[new]` `tests/test_task_state.py` — Phase C.
- `[new]` `tests/test_task_state_unblock.py` — Phase C.
- `[new]` `tests/test_emit_event.sh` — Phase D.
- `[new]` `tests/test_emit_event_concurrent.sh` — Phase D.
- `[new]` `tests/test_log_prefix_regressions.sh` — Phase D.
- `[new]` `tests/test_nag_reminder.sh` — Phase E.
- `[new]` `tests/test_nag_reminder_phases.sh` — Phase E.
- `[new]` `tests/test_worktree_registry.sh` — Phase F.
- `[new]` `tests/test_worktree_gc.sh` — Phase F.
- `[new]` `tests/test_worktree_name_validation.sh` — Phase F.
- `[new]` `tests/test_render_scenario_trace.py` — Phase G.
- `[new]` `tests/test_lint_plan_decisions.py` — Phase H.

Edited files:

- `unattended_system_instructions.md` — add §20 (compaction
  discipline, Phase A), §21 (identity recall, Phase B), §22
  (nag reminders, Phase E).
- `agents.md` — extend "Stable log prefixes (contractual)" with
  `EVENTS_EMIT`, `EVENTS_EMIT_FAIL`, `WORKTREE_REGISTER`,
  `WORKTREE_DEREGISTER`, `WORKTREE_GC`,
  `WORKFLOW_SCENARIO_TRACE_WRITTEN`,
  `TASK_STATE_UNBLOCK`, `TASK_STATE_WRITE_FAIL`,
  `TRANSCRIPT_ARCHIVE_FAIL`, `IDENTITY_REINJECT_PARSE_FAIL`,
  `NAG_REMINDER_LOAD_FAIL`,
  `WORKTREE_REGISTRY_REBUILD`,
  `WORKTREE_REGISTER_INVALID_NAME`,
  `WORKTREE_REGISTER_FAIL`, `WORKTREE_DEREGISTER_FAIL`,
  `WORKFLOW_SCENARIO_TRACE_PARSE_FAIL`. Add sub-sections for
  task-state files (Phase C) and workflow scenario traces
  (Phase G).
- `prompts/header.txt` — add `<compaction-rules>` block (Phase
  A).
- `scripts/render_prompt.sh` — render `_identity_recall.txt`
  when flag on (Phase B).
- `scripts/orchestrate_poll_process.sh` — mirror `task_state`
  writes (Phase C); wire `worktree_registry register` /
  `deregister` (Phase F); call `emit_event` mirror at every
  stable-prefix callsite (Phase D); call `nag_reminder` at the
  judge codex callsite (Phase E).
- `scripts/orchestrate_lib.py` — call `unblock_dependents` on
  issue completion (Phase C).
- `scripts/review_apply_fixes.sh` — call `nag_reminder` at editor
  callsite (Phase E).
- `scripts/review_run_reviewers.sh` — call `nag_reminder` at
  reviewer callsite (Phase E).
- `scripts/review_conflict_resolve.sh` — wire `worktree_registry`
  (Phase F).
- `scripts/gh_helpers.sh` — `emit_event` mirror at
  `AUTOFIX_PEER_CHECK` / `AUTOFIX_DISPATCH_SKIPPED` /
  `AUTOFIX_DISPATCH_ISSUED` callsites (Phase D).
- `scripts/validate_process.sh` — `emit_event` mirror at
  `AI_PHASE_FAILURE_V1` callsite (Phase D); transcript-archive
  hook (Phase A).
- `scripts/semble_helpers.sh` — `emit_event` mirror at
  `SEMBLE_QUERY` / `SEMBLE_FALLBACK` callsites (Phase D).
- `scripts/setup_serena.sh` + `scripts/serena_stats_emit.py` —
  `emit_event` mirror at `SERENA_*` callsites (Phase D).
- `scripts/collect_workflow_logs.py` — no edit; the new scenario
  trace pipeline (Phase G) runs after the collector.
- `prompts/mode-plan.txt` — append decision-schema instruction
  (Phase H).
- `prompts/mode-implement.txt`, `mode-judge.txt`,
  `mode-review-*.txt`, `mode-validate-*.txt` — no per-mode edit
  required; identity recall (Phase B) and compaction discipline
  (Phase A) are injected via `header.txt` + `render_prompt.sh`.
- `.github/workflows/workflow-log-analysis.yml` — append scenario
  trace step (Phase G).
- `.github/workflows/internal-orchestrate-poll.yml` — append
  worktree GC step (Phase F).
- `.gitignore` — add `.transcripts/`, `.tasks/`, `.events/`,
  `.worktrees/index.json`, `.ai/workflow_traces/`.

Deleted files: none.

## Data Model / Index Changes

None. None of the eight patterns touch a MongoDB collection,
index, or `/db/contracts/*.yml`. Unattended-§12 is not engaged by
this plan.

## Tests

Test strategy by category:

- **Shell smoke (`tests/test_*.sh`)** — one per new helper.
  Cover flag-on / flag-off paths, fail-open paths, and
  schema-version stamping.
- **Python unit (`tests/test_*.py`)** — Phase C (task state,
  unblock cascade), Phase G (scenario trace render), Phase H
  (lint plan decisions). Pytest, no network.
- **Regression** — Phase D's `test_log_prefix_regressions.sh`
  asserts existing stable-prefix text emissions are unchanged
  with the event mirror enabled.
- **Parity** — Phase C's mirror-parity test compares
  chunked-state reassembly to concatenated per-task files.
- **No new model-call tests** — all soft-guardrail prompts
  (Phase A, B, E) are tested at the render layer, not the
  model-response layer. End-to-end verification happens in the
  bake-out window via production logs.

Existing tests touched: none (every new test is in `tests/`
under its own file; no existing test asserts on the surfaces
touched by these phases).

## Risks & Mitigations

Cross-cutting risks (per-phase risks listed inline above):

- **R-X1: Flag sprawl.** 8 new env vars (one per phase) plus
  `UNATTENDED_NAG_SILENT_ROUNDS`, `ORCH_WORKTREE_TTL_SECS`.
  Mitigation — every flag follows the
  `<SCOPE>_<FEATURE>_ENABLED` naming convention; documented in
  README.md "Variables" table during implementation.
- **R-X2: §10 violation via accidental rename.** The plan
  preserves every existing identifier: `depends_on` (not
  `blockedBy`), `AI_MEMORY_TELEMETRY`, all 11 stable log
  prefixes, `workflow_log_collector.v2` schema, etc.
  Mitigation — implementation-phase code review specifically
  checks for renames.
- **R-X3: §14 violation via new GitHub API calls.** Phase F's
  GC pass calls `gh run list --status in_progress` once per GC
  invocation — that's one new call. Mitigation — the GC pass
  runs at the existing orchestrate-poll cadence (≈ every 5 min),
  so this is ≈ 288 calls/day; well below per-hour rate limits
  and equivalent to the per-cycle calls already audited in
  `agents.md` "Repo-specific batching helpers". No per-entry
  call; one call per GC pass.
- **R-X4: Disk bloat from 5 new cache dirs.** Mitigation — all
  five are gitignored; runbook documents weekly `find ... -mtime
  +14 -delete` cleanups; per-file size caps where applicable.
- **R-X5: Bake-out window drag.** 8 phases × 1 week each = 2
  months sequential. Mitigation — bake-outs can run in parallel
  across non-interacting phases; only C (mirror state) and F
  (worktree registry) genuinely interact with the orchestrator
  poll loop. Realistic 3–4 week total window.
- **R-X6: Drift between this plan and codex-cli upstream.**
  Phase A's compaction discipline and Phase G's parser markers
  both depend on codex-cli output behaviour. Mitigation — every
  phase fails-open; the discipline is a soft guardrail; the
  parser markers are centralised at the top of a single file.

## Rollout

Per-phase rollout follows the same template:

1. **Land the helper + wired callsites with flag default `false`.**
   The PR includes the new test files and updates `agents.md` /
   `unattended_system_instructions.md` / README.md as applicable.
2. **Bake-out window (1 production week).** Flag stays off; the
   helper compiles and the wiring is exercised by test runs.
   Production behaviour is unchanged.
3. **Manual canary.** Flip the flag on for one phase / one
   workflow run; verify the expected on-disk artefact appears
   (`.events/...jsonl`, `.tasks/...json`, etc.) and that
   `git status --short` outside cache paths is clean.
4. **Flip default.** Follow-up PR titled `chore: enable
   <FLAG> by default` flips the default in the helper's
   `${FLAG:-false}` expansion. If a regression appears, revert
   the chore PR — the helper still works, just with the flag
   off.
5. **Cleanup phase.** After all 8 flags default on, a final
   PR audits whether the flag mechanism itself can be removed
   (in cases where the behaviour is unambiguously safe). Most
   flags will stay as kill-switches; that is the
   `AI_MEMORY_ENABLED` precedent.

No consumer-repo propagation. Q3:A locked the surface to the
unattended pipeline; `workflow-templates/` is unaffected. If a
consumer later wants any of these patterns, a follow-up plan
adds them to `workflow-templates/` and propagates via
unattended-§19.

## Decisions

(Example block per Phase H's convention.)

### D1 — Mirror vs. replace orchestrator state (Phase C)

- **Chosen:** mirror — write `.tasks/<wave>/<issue>.json` in
  addition to the existing chunked-state comment.
- **Alternatives considered:**
  - **Replace** — drop the chunked-state comment path entirely
    and read from `.tasks/*.json`. *Rejected because:* breaks
    wave recovery if `.tasks/` is wiped between runs; violates
    §9 (extend, don't replace); the chunked-state comment is
    the durable across-CI-run anchor (issue comments persist;
    repo-root caches do not).
  - **Defer** — keep current chunked state, postpone
    file-per-task to a future plan. *Rejected because:* a
    future plan would lose the flag-gated parity bake-out
    window; we'd be writing the layout cold with no production
    diff data.
- **Why:** §9 + §10 compliance; reversible via flag; gives a
  bake-out window before any read-path cut-over.

### D2 — `depends_on` vs. `blockedBy` field name (Phase C)

- **Chosen:** keep `depends_on` (and `reissue_depends_on`).
- **Alternatives considered:**
  - **Adopt `blockedBy` from upstream harness.** *Rejected
    because:* §10 forbids renaming existing identifiers without
    explicit instruction; `depends_on` is the established key
    in `orchestrate_poll_process.sh` and
    `orchestrate_state_v2.py`; adopting both would create a
    confusing two-name regime.
- **Why:** naming immutability per §10.

### D3 — Mirror vs. replace text log prefixes (Phase D)

- **Chosen:** mirror — every existing text-prefix emission
  remains; the JSONL stream is an additional sink.
- **Alternatives considered:**
  - **Replace** — move all stable-prefix emissions to JSONL,
    drop the stderr text lines. *Rejected because:* §10 — the
    11 prefixes in `agents.md:130-147` are contractual; the
    workflow-log-analysis pipeline (`collect_workflow_logs.py:45-63`)
    greps for them in text logs; renaming or removing them is
    breaking.
- **Why:** §10 + §9.

### D4 — Single events.jsonl vs. per-prefix file (Phase D)

- **Chosen:** single `.events/run-<run_id>.jsonl` per workflow
  run.
- **Alternatives considered:**
  - **Per-prefix files** (`.events/run-<run_id>/LABEL_REPAIR.jsonl`).
    *Rejected because:* more files, more inode churn, harder
    to grep across phases; the harness's own `events.jsonl` is
    flat.
  - **Append to a single repo-wide events.jsonl across runs.**
    *Rejected because:* concurrent writes across runs are
    error-prone; per-run files are naturally isolated.
- **Why:** simplicity + isolation.

### D5 — Nag reminder at wrapper vs. prompt level (Phase E)

- **Chosen:** wrapper-level injection.
- **Alternatives considered:**
  - **Prompt-only ("after N silent rounds, …")** — let the
    model self-monitor. *Rejected because:* the model can't
    reliably count its own silent rounds across compactions;
    wrapper-side detection is deterministic.
  - **Codex-cli internal hook.** *Rejected because:* upstream
    surface change; out of scope.
- **Why:** determinism; works without upstream changes.

### D6 — Worktree registry vs. ephemeral worktrees (Phase F)

- **Chosen:** registry — track in `.worktrees/index.json`.
- **Alternatives considered:**
  - **Pure ephemeral** (current behaviour). *Rejected
    because:* stale worktrees after killed runs accumulate;
    `git worktree list --porcelain` is the only recovery
    path, and it doesn't carry our `task_id` / `owner_phase`
    metadata.
  - **Database-backed registry** in MongoDB. *Rejected
    because:* operational complexity; §12 doesn't justify it
    for repo-local filesystem state.
- **Why:** registry adds observability + GC without changing
  the ephemeral-by-default semantics.

### D7 — Scenario trace shape: harness-compatible vs. custom (Phase G)

- **Chosen:** harness-compatible
  (`{type, content}` step shape with `user_message |
  assistant_text | tool_call | tool_result`).
- **Alternatives considered:**
  - **Custom shape** tailored to our log format. *Rejected
    because:* harness shape is widely-used; using it
    means a future teaching visualiser could consume our
    traces without translation.
- **Why:** ecosystem fit + zero benefit to invention.

### D8 — Decision-schema enforcement: linter warning vs. CI fail (Phase H)

- **Chosen:** warning-only.
- **Alternatives considered:**
  - **CI-failing** when a plan PR omits `## Decisions`.
    *Rejected because:* §16 (output contract) and §17
    (forbidden behaviours) both lean toward "don't block on
    advisory checks"; existing plans don't all have the
    section; legacy retrofit would be a separate housekeeping
    PR.
- **Why:** advisory adoption first; tighten later only if drift
  observed.

## Open Questions

These survived the clarification round and need a reviewer
decision before implementation kicks off:

- **OQ-A1.** Phase A's transcript archive currently captures
  codex-cli's `--show-raw-conversation` output (or equivalent).
  Does codex-cli actually expose a flag that dumps the full
  raw conversation including system + assistant + tool calls?
  If not, what's the closest available capture
  (`--debug-transcript`?), and is the resulting JSON shape
  stable enough to commit to `transcript_archive.v1.json`?
- **OQ-A2.** The compaction-discipline §20 in
  `unattended_system_instructions.md` is a soft guardrail. Do
  we want a follow-up plan to wire it into codex-cli via a
  pre-compaction hook (if codex-cli grows one), or leave it as
  prompt-only forever?
- **OQ-C1.** Phase C's task-state file location:
  `.tasks/<wave_id>/<issue_id>.json` or
  `.tasks/<issue_id>.json` flat? Flat is simpler for
  cross-wave queries; nested matches the harness layout and
  avoids `.tasks/` becoming huge over time.
- **OQ-C2.** Should `unblock_dependents` also update the
  chunked-state comment (so the two stores stay in sync
  byte-for-byte)? Mirror-only writes risk drift between the
  cascade timing in `orchestrate_lib.py` and the next poll
  cycle's chunked-state re-serialisation.
- **OQ-D1.** Phase D's `emit_event` records a `phase` field —
  what canonical phase names do we use? The agents.md "Workflow
  architecture" section lists 12 phases. Should `phase`
  enumerate exactly those 12, or use a more granular set
  (e.g., `review-reviewer-pass-1` vs. `review-reviewer-pass-2`)?
- **OQ-D2.** Should the JSONL stream include `AI_MEMORY_TELEMETRY`
  events (currently emitted as `AI_MEMORY_TELEMETRY: <JSON>`
  text lines)? Treating them as events would consolidate; not
  treating them preserves the existing memory-system telemetry
  pipeline in `scripts/analyze_workflow_logs.py`.
- **OQ-E1.** Phase E's nag reminder defaults to 3 silent rounds.
  Is that right for our `xhigh` reasoning tier (where one
  "silent" round can be 5+ minutes of reasoning), or should the
  default be higher (e.g., 5)?
- **OQ-E2.** Should the nag-reminder injection be a
  user-role message or a system-role message? The harness uses
  user-role. System-role is more aggressive but harder to
  retract.
- **OQ-F1.** GC criterion includes `gh run list --status
  in_progress`. What if the workflow run is `queued`? Are
  those owner_run_ids still considered "in use"?
- **OQ-F2.** Should `worktree_registry register` reject a
  duplicate `task_id` (i.e., enforce one worktree per task), or
  allow multiple worktrees on the same task with different
  branches? The harness enforces uniqueness by `name`; we use
  `task_id` as the join key, so multiple worktrees on one task
  may be valid (e.g., conflict resolution + integration
  testing).
- **OQ-G1.** Phase G's scenario trace runs downstream of
  `collect_workflow_logs.py`. Should it run only when that
  collector succeeds, or independently from raw workflow logs?
  The collector already does retry/backoff; running downstream
  is simpler.
- **OQ-G2.** Should the scenario trace be uploaded anywhere
  (S3, GitHub artefact) or stay strictly local? This plan
  keeps it local; a future plan could add an upload step.
- **OQ-H1.** Should the existing 4 plans (`apply-ai-tools-learnings`,
  `ai-code-review-learnings`, `complete-squad-improvements`,
  `judge-loop-and-reissue`) be retro-edited to include
  `## Decisions`? Or do we accept them as legacy and apply the
  convention only to new plans?
- **OQ-X1.** Per-phase landing PR rule — do we want to land all
  8 phases under this single plan branch, or split into 8
  independent PRs targeting the default branch directly?
  Splitting matches the per-phase bake-out cadence; single-PR
  matches the "one plan, one rollout" pattern of prior plans.

## References

- Upstream repo: `https://github.com/shareAI-lab/learn-claude-code`.
- Upstream stages cited: `agents/s03_todo_write.py` (Phase E),
  `agents/s06_context_compact.py` (Phase A),
  `agents/s07_task_system.py` (Phase C),
  `agents/s11_autonomous_agents.py` (Phase B),
  `agents/s12_worktree_task_isolation.py` (Phases D + F).
- Upstream web data shapes:
  `web/src/data/scenarios/sXX.json` (Phase G),
  `web/src/data/annotations/sXX.json` (Phase H).
- In-repo prior "external learnings" plans (template precedent):
  - `docs/plans/apply-ai-tools-learnings-plan.md` — prompt-only
    additive borrow.
  - `docs/completed/ai-code-review-learnings-plan.md` — flag-gated,
    fail-open phased rollout (closest structural sibling).
- In-repo constraint sources:
  - `unattended_system_instructions.md` — §8 (env-var
    defaults), §9 (minimal change set), §10 (naming
    immutability), §11 (code style), §13 (repository hygiene),
    §14 (GitHub API hygiene), §16 (output contract).
  - `agents.md` — Workflow architecture, Models in use, Stable
    log prefixes (contractual).
  - `CLAUDE.md` — out-of-scope (Q3: A); cited only to mark the
    interactive surface as untouched.
- In-repo current-state evidence (sample callsites):
  - `scripts/codex_model_catalog.json:24-30` —
    `auto_compact_token_limit` per model (Phase A).
  - `prompts/header.txt:1` + `prompts/mode-*.txt:1` — per-phase
    `Role: ... Goal: ...` lines (Phase B).
  - `scripts/orchestrate_state_v2.py:1-61` — chunked-state
    persistence (Phase C).
  - `scripts/orchestrate_poll_process.sh:~10767-10829` —
    `depends_on` array handling + ad-hoc `git worktree` usage
    (Phases C + F).
  - `agents.md:130-147` — 11 stable log prefixes (Phase D
    contract anchor).
  - `scripts/ai_memory.py:49` — `AI_MEMORY_TELEMETRY: <JSON>`
    emission pattern (Phase D precedent).
  - `scripts/collect_workflow_logs.py:45-63` — `RETRY_MARKERS`
    + `LOG_EXCERPT_MAX_CHARS = 4000` (Phase G context).
  - `docs/plans/complete-squad-improvements-plan.md:189-198` —
    "Alternatives considered" prose section (Phase H starting
    point).
