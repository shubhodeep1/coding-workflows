# Plan: Serena MCP Integration Across the Unattended Pipeline

> **Status**: NOT STARTED — this branch (`claude/serena-integration-plan-qBL0K`)
> ships the design only. No `scripts/setup_serena.sh`, no
> `scripts/mcp_handshake_probe.py`, and no `[mcp_servers.serena]` block exist
> in the current tree (verified: `grep -rni serena scripts/ prompts/
> .github/workflows/ workflow-templates/` returns only two capitalized
> "Serena/MCP" comments in `.github/workflows/test-and-mark-stable.yml`
> (lines 1082 and 3020) and no other matches). The `[Unreleased]` block in
> `CHANGELOG.md` and several `analysis/workflow-optimization-2026-05-0*.md`
> files describe this integration as if it has shipped (e.g. "269 Serena
> tool calls", "94% efficiency", "~85% token savings"); those are
> aspirational / forward-looked entries and are reconciled in §12.
> **Owner**: solo developer (sole user of the workflows in this repo).
> **Upstream**: <https://github.com/oraios/serena> — Serena is an LSP-backed
> coding agent toolkit exposed via MCP. Its `find_symbol`,
> `find_referencing_symbols`, `replace_symbol_body`, `insert_after_symbol`,
> and `read_memory` tools let a model navigate and edit code at symbol
> granularity instead of paying for whole-file reads.
> **Goal**: replace the codex-cli editor's grep+read code-discovery loop with
> Serena's MCP-served symbol tools across every code-touching unattended
> phase. Empirical evidence from prior measurements (recorded in
> `analysis/workflow-optimization-2026-05-02-3.md`) puts the per-`implement`
> savings at ~85% of editor input tokens (~26k with Serena vs ~181k without
> on a single sample). This plan brings that pattern in for real and
> propagates it to validate, review autofix, conflict resolver, and the
> implement-diagnose / implement-repair siblings.
> **Scope decisions (locked)**:
>
> - **Q1 — Phase coverage**: every code-touching unattended phase. In scope:
>   `implement`, `implement-diagnose`, `implement-repair`,
>   `implement-repair-syntax`, `validate` (discover / diagnose / self-heal /
>   fix-harness), `review_autofix` (editor + reviewers + consolidator),
>   `conflict-resolver`, `integration-sync-conflict-resolver`. Out of scope:
>   `clarify`, `clarify-respond`, `plan`, `judge`, `orchestrate`,
>   `orchestrate-poll`, `orchestrate-poll-judge`, `workflow-log-analysis`,
>   `memory-maintenance` — these phases read issue / PR / log text, not
>   source. Bootstrapping Serena there pays setup cost for ~zero savings.
> - **Q2 — Delivery shape**: MCP server registered in
>   `~/.codex/config.toml` as `[mcp_servers.serena]`, gated behind a
>   pre-flight JSON-RPC `initialize` handshake probe (the same defence the
>   `[Unreleased]` CHANGELOG describes for Context7 and Git MCP). The
>   editor stays on `apply_patch_tool_type = "function"` per the 2026-05-07
>   ablation pinned in `agents.md`; Serena tools are exposed *alongside*
>   `apply_patch`, never in place of it. CLI invocation from shell scripts
>   is rejected — Serena's value is the agent-loop semantic search, not
>   one-shot retrieval.
> - **Q3 — Track**: unattended pipeline only. Interactive Claude Code
>   sessions are out of scope (interactive sessions already have Read /
>   Grep / Agent and `CLAUDE.md` is the system context for that surface,
>   not this document). Mirror of the semble-plan precedent.
> - **Q4 — Plan doc location and format**: this file
>   (`docs/serena-integration-plan.md`), comprehensive design-doc shape
>   matching `docs/semble-integration-plan.md`.

This plan deliberately stays inside the existing pipeline architecture —
prompt prefix caching, `targeted_file_context.py` inlining, codex-cli with
`apply_patch` (function-typed), the central `write_codex_config.sh` writer,
the Semble retrieval path that landed in 2026-05 — and adds Serena as an
*additional* tool surface for the editor, not a replacement for any of those
layers. Removing whole-file inlining or grep is explicitly out of scope.

---

## 1. Background

### 1.1 What Serena is (and isn't)

Serena is an MCP server that wraps language servers (Python, TypeScript,
Go, Rust, Java, C#, …) and exposes a uniform symbol-level toolset:

- `find_symbol` / `find_referencing_symbols` — LSP-backed code navigation
  that returns just the matching symbol body (or a header summary), not
  the surrounding file.
- `read_file` / `list_dir` — unchanged from the model's existing read
  tool, but with token-cheaper formatting.
- `replace_symbol_body` / `insert_after_symbol` / `insert_before_symbol`
  — surgical edits scoped to one symbol; the model never has to send
  the surrounding file context back as part of an `apply_patch` payload.
- `write_memory` / `read_memory` / `list_memories` — small project-scoped
  notes the model can leave for itself across turns.

Serena is **not** a chunk-retrieval engine (that role is filled by
Semble — see §1.4 for how the two compose). It does not produce
embeddings; symbol resolution is exact, courtesy of the LSP layer.

### 1.2 Current state of Serena in this repo

| Surface | Current state |
|---|---|
| `scripts/setup_serena.sh` | **does not exist** |
| `scripts/mcp_handshake_probe.py` | **does not exist** |
| `[mcp_servers.serena]` in `write_codex_config.sh` | **not emitted** |
| `.serena/project.yml` | **does not exist** (Serena writes / uses this at runtime) |
| `.gitignore` `.serena/` rule | **already present** — `.gitignore:6` lists `.serena/`; no Phase 1 edit needed in this repo (consumer-repo install guidance still recommends the rule) |
| `setup-uv` step | already present in `implement.yml`, `validate.yml`, `review_autofix.yml`, `clarify.yml`, `plan.yml`, `orchestrate*.yml` (see §1.5) |
| `tests/test_mcp_handshake_probe.py` | **does not exist** |
| `tests/fixtures/mcp_handshake/` | **does not exist** |
| `agents.md` "Stable log prefixes" — `SERENA_*` entries | not present |
| `CHANGELOG.md [Unreleased]` references to Serena | **present**, but aspirational on this branch (§12) |
| `analysis/workflow-optimization-2026-05-0*.md` references | present, projecting / re-stating the integration shape (§12) |

The ground truth is that this branch is greenfield with respect to
Serena. Every artefact named in the CHANGELOG `[Unreleased]` block needs
to be created by Phase 1 of this plan. §12 (CHANGELOG / analysis
reconciliation) discusses how to handle the carry-over text.

### 1.3 Where the editor's tokens go today

In the unattended pipeline as it stands (semble landed; Serena hasn't),
the editor's input tokens come from three layers:

1. **Static prefix** (`scripts/build_static_context.sh`) — system
   instructions + `agents.md` + phase-trimmed `ai_pipeline.md` + README
   trim. Identical across runs of a given phase, so OpenRouter prefix-
   caches it.
2. **Targeted file inlining** (`scripts/targeted_file_context.py`) —
   plan-named files inlined verbatim, capped at 100 KB total. Overflow
   files are now Semble-chunk-fetched (per `docs/semble-integration-
   plan.md` Phase 2).
3. **Codex-driven exploration at runtime** — for everything not covered
   by layers 1 and 2, codex uses its built-in `read` and shell-`grep`
   tools, paying full token cost for every line surfaced. Each `read`
   is a whole file (or a 200-line slice with no awareness of symbol
   boundaries); each `grep` returns lines plus context that the model
   then has to `read` for understanding.

Layer 3 is where Serena replaces the loop:

- `find_symbol "ensureIndexes"` returns the function body and signature,
  not the 600-line file containing it.
- `find_referencing_symbols` returns a list of caller-site symbols, not
  a `grep` of the bare token (which matches comments, strings, tests,
  and unrelated identifiers).
- `replace_symbol_body` lets the model emit a small JSON tool call
  rather than a full-file `apply_patch` envelope.

The 2026-05-02-3 analysis sample shows `implement` runs at **94% Serena
efficiency** (269 Serena tool calls vs 14–16 file-based fallback ops)
and **~26,350 tokens with Serena vs ~181,100 without** — an 85% input-
token reduction at the editor surface for code-discovery work. Those
numbers are projections on this branch (Serena is not yet wired here),
but the upstream evaluation table corroborates them across the
language mix this repo and its consumers operate in.

### 1.4 Why Serena composes with Semble (not replaces it)

The two tools cover different layers of the same problem:

- **Semble** (already shipped, see `docs/semble-integration-plan.md`)
  pre-fetches *bounded text chunks* into the dynamic prompt before
  `codex exec` runs. Output is static text that the model reads at
  prompt-load time. Cheap, deterministic, requires no agent loop.
- **Serena** (this plan) gives the model *interactive* symbol-level
  tools at runtime. It only fires when the model's reasoning loop
  decides it needs more information. Higher per-call cost than Semble
  (because it requires the model to issue a tool call and consume the
  response), but unbounded in scope — the model can chase a definition
  through a 50-file inheritance chain.

The planned composition for an `implement` run is:

1. Static prefix (cached).
2. `targeted_file_context.py` inlines plan-named files, with Semble
   filling overflow.
3. `apply_patch` is the editor's primary write tool; Serena's
   `replace_symbol_body` is an additive write tool the model can
   choose when the edit is symbol-scoped.
4. `find_symbol` / `find_referencing_symbols` are the read-loop tools.
   Codex's built-in `read` and `grep` remain as fallbacks (Serena's
   stats show ~6% of operations still fall back to file reads).

### 1.5 Why MCP delivery (and not CLI like Semble)

Semble was wired CLI-only because its job is one-shot retrieval that
fits naturally into the prompt-assembly path: a shell script runs
`semble query`, captures stdout, appends to the dynamic prompt, exits.

Serena's value is the *agent loop*. The model has to be able to issue a
`find_symbol` call, get a response, decide based on the response whether
to call `find_referencing_symbols` next, etc. CLI-only delivery would
require us to either pre-compute the symbol queries before `codex exec`
(losing the agent loop, which is the whole point) or wrap codex in a
shell loop that re-invokes it after each Serena query (a custom client
the codex-cli team has not built and we should not reinvent).

MCP is the supported pathway. Codex 0.113+ supports `[mcp_servers.*]`
blocks in `~/.codex/config.toml`; codex registers them at startup,
performs an `initialize` handshake, and exposes their tools to the
model alongside `apply_patch`.

### 1.6 The handshake probe constraint

The `[Unreleased]` CHANGELOG block already describes the failure mode
that motivates the handshake probe: when an MCP server fails the
`initialize` exchange (timeout, EOF mid-handshake, malformed/error
response, id mismatch), codex still emits a `tools[N]` entry whose
`function` field is `undefined`. Some OpenRouter back-ends — notably
Azure — reject those payloads with HTTP 400, taking down the entire
implement / validate / review_autofix retry loop with one MCP server's
flake.

The `[Unreleased]` block describes a probe (`scripts/mcp_handshake_
probe.py` + `probe_mcp_handshake` helper in `scripts/setup_serena.sh`)
that performs the JSON-RPC `initialize` exchange *before* writing the
`[mcp_servers.<name>]` block. Servers that fail the probe are omitted.
Phase 1 of this plan is responsible for actually creating those files.

### 1.7 Why not just lean on apply_patch alone

The 2026-05-07 ablation (referenced in `agents.md`) pinned editor
reliability to `apply_patch_tool_type: function`. That fixed the
"announce-without-emit" failure mode (codex#11151) but did nothing
about the editor's *exploration* cost: a typical `implement` run
spends most of its input tokens not on emitting `apply_patch` calls
but on `read`-ing files to understand what to patch. Serena targets
that exploration cost. The editor still uses `apply_patch` as its
primary write tool; Serena's symbol tools complement it.

---

## 2. Goal and non-goals

### 2.1 Goal

For every phase listed in the Q1 scope (§0):

1. Register a `[mcp_servers.serena]` block in `~/.codex/config.toml` so
   the codex-cli editor sees Serena's tools alongside `apply_patch`.
2. Pre-flight every Serena registration with a JSON-RPC `initialize`
   handshake probe; on probe failure, omit the block and continue with
   the legacy grep+read path. **The pipeline must never deadlock on
   Serena unavailability.**
3. Bootstrap Serena lazily — only after every cheap short-circuit
   (label gates, no-linked-issue exits, comment-only review paths) —
   per the recommendation in
   `analysis/workflow-optimization-2026-05-01-2.md`.
4. Emit compact, contractual `SERENA_*` log prefixes for every phase
   that bootstraps Serena, so the workflow-log-analysis pipeline can
   measure actual contribution and detect regressions.
5. Preserve Serena stats across cancellation / cleanup so cancelled
   review runs stop logging "No Serena tool usage stats found"
   (gap called out in `analysis/workflow-optimization-2026-05-02-3.md`).
6. Propagate to consumer repos through the reusable workflows under
   `.github/workflows/`; consumer-side `workflow-templates/*.yml`
   wrappers stay as `uses:`-only callers and do not change. Uptake
   requires a separate `@stable` release dispatch via
   `.github/ai/consumer_repos.json` (per `CLAUDE.md` §14).

### 2.2 Non-goals

- **Not changing the Semble integration.** Semble continues to handle
  static-prompt overflow (`docs/semble-integration-plan.md` Phase 2)
  and reviewer / conflict-resolver / validate / judge prompt prefill.
  Serena adds runtime symbol tools to the editor; the two compose
  without overlap.
- **Not changing `apply_patch_tool_type`, model slugs, reasoning
  effort, or any other element of the editor configuration.** The
  2026-05-07 ablation outcome (function-typed `apply_patch`) is
  load-bearing.
- **Not changing the static-prefix shape.** `build_static_context.sh`
  output stays byte-identical for the static prefix layer. Serena's
  presence is conveyed only via `~/.codex/config.toml` and a small
  prompt-side hint section in the phase-specific dynamic context (§3).
- **Not extending Serena to interactive Claude Code.** Excluded by Q3.
- **Not introducing a CLI-only invocation path for Serena.** Excluded
  by Q2 (option B). The MCP path is the only delivery shape.
- **Not enabling Serena on metadata-only phases** (clarify, plan,
  judge, orchestrate, workflow-log-analysis, memory-maintenance).
  Excluded by Q1 (option B / D were rejected). Those phases never
  bootstrap Serena and pay no setup cost.
- **Not wiring Serena into the per-reviewer codex-home in
  `review_run_reviewers.sh`.** That script strips every
  `[mcp_servers.*]` table from the per-reviewer config (lines 193,
  200) on purpose — reviewers receive a fixed diff bundle and should
  not run their own exploration loop. The editor (post-consolidation
  fix-up) is where Serena lives within the review_autofix pipeline.
  See §3.3 and §4.5.

### 2.3 Acceptance criteria

The plan is "implemented" when, on the unattended pipeline:

1. Every phase in the Q1 scope either has Serena registered and
   probed, or has a documented reason for skipping it.
2. `cost_audit.py` shows a measurable reduction in per-issue editor
   input tokens on at least three real issues across at least two
   consumer repos.
3. `SERENA_QUERY`, `SERENA_FALLBACK`, and `SERENA_PROBE` log prefixes
   appear in `agents.md` "Stable log prefixes (contractual)" and are
   emitted by every phase that bootstraps Serena.
4. Cancelled review runs preserve their Serena stats — no "No Serena
   tool usage stats found" lines on cancelled or short-circuited paths
   that genuinely bootstrapped Serena.
5. Lazy bootstrap is verifiable: the `Setup Serena` step runs *after*
   the gate steps that decide whether the workflow has work to do, so
   short-circuit runs (e.g. ~10–28s `review_autofix` no-op paths) do
   not pay Serena setup cost.
6. Consumer-repo propagation is in place: at least one `@stable`
   release has shipped the new reusable-workflow shape, and a
   follow-up issue has been processed end-to-end on a consumer repo
   with Serena enabled.

---

## 3. Integration surface (catalog)

This catalog is the working list for Phases 2–4. Each entry names the
existing exploration shape, the proposed Serena surface, and the
fallback trigger. **Every entry assumes Phase 1 plumbing is in place.**

### 3.1 `implement` — `prompts/mode-implement.txt` + `.github/workflows/implement.yml`

**Today**: codex receives the static prefix + targeted-file inline
context + Semble overflow chunks; for anything not covered, it uses
its built-in `read` (whole files) and shell-`grep`. Mid-edit, it
repeats the read loop to verify each change.

**With Serena**: `find_symbol` is the canonical "what does this
function look like" tool; `find_referencing_symbols` replaces the
grep-then-read pattern for caller discovery; `replace_symbol_body` is
an additive write path the model may choose for symbol-scoped edits.
Apply_patch remains the primary editor for multi-symbol or non-symbol
edits (e.g. config files, prose).

**Prompt edit**: append a short "Tool guidance" subsection to
`prompts/mode-implement.txt` listing Serena tools available when
`SERENA_AVAILABLE=true` is exported. Hint section is wrapped in a
template placeholder `{{SERENA_TOOL_HINTS}}` (rendered by
`render_prompt.sh`) so it disappears entirely when Serena is off
— preserving prompt-byte-identical fallback.

**Fallback**: `SERENA_AVAILABLE=false` makes the placeholder render
empty and the `[mcp_servers.serena]` block is not written; codex
operates exactly as today. Log
`SERENA_FALLBACK target=implement reason=<probe-failure|setup-failure|disabled>`.

### 3.2 `implement-diagnose` and `implement-repair` (+`-syntax`) — diagnose/repair scripts and prompts

Files: `scripts/implement_diagnose_post_codex_failure.sh`,
`prompts/mode-implement-diagnose.txt`,
`prompts/mode-implement-repair.txt`,
`prompts/mode-implement-repair-syntax.txt`.

**Today**: diagnose reads the codex failure tail and the plan-named
files; repair reads the partially-edited files. Both phases inherit
the implement step's `read`/`grep` exploration loop. Semble pre-fills
chunk context for the failure-tail identifier set.

**With Serena**: the diagnose/repair phases run inside the same
`implement.yml` job (via `MODE=implement-repair` / `implement-repair-
syntax` reasoning passes — see `implement.yml:1869`-style in-place
config patches). The `[mcp_servers.serena]` block written for the
main implement pass is reused unchanged. The repair-phase prompt gains
a small hint that calling `find_referencing_symbols` on the failing
identifier is preferred over re-grep.

**Prompt edit**: add `{{SERENA_TOOL_HINTS}}` to the three repair-phase
prompts. The placeholder renders identically across implement /
diagnose / repair so the model learns one mental model.

**Fallback**: identical to §3.1 — same flag, same bypass path.
Log `SERENA_FALLBACK target=implement-diagnose|implement-repair
[reason=...]`.

### 3.3 `review_autofix` — editor (post-consolidation), reviewers, consolidator

Files: `.github/workflows/review_autofix.yml`,
`scripts/review_run_reviewers.sh`,
`scripts/review_apply_fixes.sh`,
`scripts/review_consolidate.sh`.

**Today**: reviewers receive a fixed diff bundle plus Semble pre-
fetched chunks (per `docs/semble-integration-plan.md` Phase 3); they
do not run an exploration loop. The post-consolidation editor *does*
re-explore when applying multi-file fixes; it inherits the
implement-style grep+read loop.

**With Serena**: only the **post-consolidation editor pass**
(`review_apply_fixes.sh` → `codex exec`) gets Serena registered. The
per-reviewer codex-home in `review_run_reviewers.sh` continues to
strip `[mcp_servers.*]` (lines 193, 200 of that script — keep this
behaviour, do not regress it). Reasoning: reviewers operate on a
bounded diff with Semble-prefetched context; giving them a full
exploration toolset would inflate review tokens without correctness
gain. The editor pass is where the cross-symbol fix-up happens, and
that is where Serena's symbol tools pay back.

**Prompt edit**: editor prompt assembled by `review_apply_fixes.sh`
gets the `{{SERENA_TOOL_HINTS}}` placeholder (same content as in
§3.1). Reviewer / consolidator prompts are *not* modified.

**Fallback**: editor pass falls back to grep+read when
`SERENA_AVAILABLE=false`. Log
`SERENA_FALLBACK target=review-autofix-editor pr=<num>`.

### 3.4 `validate` — discover / diagnose / self-heal / fix-harness

Files: `.github/workflows/validate.yml`, `scripts/validate_driver.sh`,
`scripts/validate_process.sh`, `scripts/self_heal_validation.sh`,
`prompts/mode-validate-discover.txt`,
`prompts/mode-validate-diagnose.txt`,
`prompts/mode-validate-self-heal.txt`,
`prompts/mode-validate-fix-harness.txt`.

**Today**: validate phases scan candidate harness files via grep
patterns (`describe(`, `def test_`, etc.) then `read` the top
matches into the prompt. Semble fills chunk context per
`docs/semble-integration-plan.md` Phase 4.

**With Serena**: register `[mcp_servers.serena]` for the validate
codex-cli passes that mutate code (`self-heal`, `fix-harness`).
`discover` and `diagnose` are dominantly read-only / metadata-shaped;
they get Serena tools registered but are unlikely to invoke them
heavily — register anyway, since the bootstrap cost is amortised
once per job.

**Prompt edit**: add `{{SERENA_TOOL_HINTS}}` to all four validate
prompts. The hints emphasise `find_symbol` for failing-test
identifier lookup (a common pattern in self-heal) and
`replace_symbol_body` for fix-harness's targeted edits.

**Fallback**: identical to §3.1. Log
`SERENA_FALLBACK target=validate phase=<discover|diagnose|self-heal|fix-harness>`.

### 3.5 Conflict resolver — `prompts/conflict-resolver.txt`, `prompts/integration-sync-conflict-resolver.txt`, `prompts/integration-sync-conflict-resolver-retry-prelude.txt`, `scripts/review_conflict_resolve.sh`, `scripts/review_conflict_prepare.sh`

**Today**: the resolver receives both sides of the conflict + a
Semble query keyed on the affected symbols (per `semble-integration-
plan.md` Phase 3). It then `read`s nearby callers to decide on the
merge.

**With Serena**: the resolver gets `find_referencing_symbols` to find
callers of the conflicted identifier, scoping the merge decision to
the actual touch surface instead of relying on the model's grep
intuition. `replace_symbol_body` is *not* used in the resolver path —
conflict markers must be resolved via `apply_patch` so the merge
shows up as a normal commit.

**Prompt edit**: add `{{SERENA_TOOL_HINTS_RESOLVER}}` placeholder
(distinct from the editor placeholder because the resolver is
forbidden from using Serena's *write* tools; the hint section
explicitly limits the menu to read-only Serena tools).

**Fallback**: existing Semble-prefetch + grep+read path remains.
Log `SERENA_FALLBACK target=conflict-resolver pr=<num>` /
`SERENA_FALLBACK target=integration-sync-conflict-resolver pr=<num>`.

### 3.6 Sites that stay grep-only

These are *not* code-context surfaces; Serena is not a fit:

- `scripts/render_prompt.sh` — placeholder substitution.
- `scripts/build_static_context.sh` — file concatenation.
- `prompts/mode-validate-generate.txt` — embedded shell that
  grep-checks CI log output.
- `prompts/mode-workflow-api-redundancy.txt` mentions of `grep` —
  log-key conventions, not literal calls.
- All `gh_helpers.sh` / `label_helpers.sh` / `memory_helpers.sh`
  greps — they parse CLI output, not source code.
- Metadata-only phases listed in §0 Q1 (clarify, plan, judge,
  orchestrate, workflow-log-analysis, memory-maintenance).

These will be skipped explicitly with a one-line comment in the
rollout PR so future readers see the decision was deliberate.

---

## 4. Architecture

### 4.1 Component layout

```
+----------------------------------------------------------------+
| GHA job (codex-cli phase, e.g. .github/workflows/implement.yml)|
|                                                                |
|  1. actions/checkout (fetch-depth: 0)                          |
|  2. setup-uv (already present)                                 |
|  3. Cheap gates run first (label gate, no-linked-issue exit,   |
|     comment-only check). Workflow may short-circuit here       |
|     paying NO Serena cost.                                     |
|  4. setup_serena.sh (NEW):                                     |
|       - Install Serena via uv (pinned version)                 |
|       - Write .serena/project.yml                              |
|       - Probe MCP handshake (15s timeout)                      |
|       - On probe success → write [mcp_servers.serena] block    |
|         into ~/.codex/config.toml (post write_codex_config.sh) |
|       - On probe failure → SERENA_AVAILABLE=false, no block    |
|  5. write_codex_config.sh runs as today; the Serena block is   |
|     appended *after* its output (not edited into its emit      |
|     path, so the central writer's tests stay green).           |
|  6. build_static_context.sh <phase> <static.txt>               |
|  7. render_prompt.sh fills {{SERENA_TOOL_HINTS}} based on      |
|     SERENA_AVAILABLE.                                          |
|  8. codex exec ... < <(cat static.txt dynamic.txt)             |
|       - codex initialises the Serena MCP server                |
|       - Model can call find_symbol / replace_symbol_body / ... |
|         alongside apply_patch                                  |
|  9. End-of-job stats collection: copy Serena's local stats     |
|     file out of the runtime tree BEFORE cancel/cleanup wipes   |
|     it; emit SERENA_QUERY / SERENA_FALLBACK summary lines.     |
+----------------------------------------------------------------+
```

### 4.2 Install path

Serena is installed inside `scripts/setup_serena.sh` via `uv` (already
staged in every in-scope workflow):

- Pinned version: `serena-agent==<X.Y.Z>` (exact pin TBD on first
  stable cut; see §9 open questions).
- Idempotent: skip install if `which serena-agent` resolves and
  reports the pinned version.
- Fail-soft: on install failure, set `SERENA_AVAILABLE=false` in
  `$GITHUB_ENV` and exit 0. Every Serena caller respects this flag
  and falls back without the per-site failure-log noise.
- Optional cache: `~/.cache/uv` is already cached by `setup-uv` in
  the existing workflows; Serena's wheel benefits from that cache
  with no plan-side change.

### 4.3 `.serena/project.yml` lifecycle

Serena requires a `.serena/project.yml` at the project root to define
language(s), include/exclude globs, and tool exposure. The file is:

- **Generated by `setup_serena.sh` at runtime** (not committed). Per
  the language-coverage decision below, the generated file is
  identical across consumer repos: every Serena-supported language is
  enabled, so a single template covers Python-only, JS-only,
  multi-language, and unknown-language repos without per-repo
  detection logic.
- **All Serena-supported languages enabled by default.** Rationale:
  this plan ships into multiple consumer repos with different
  (and changing) language mixes. A file-extension census at bootstrap
  is fragile (misses bootstrapping repos, generated code, scripted
  files inside data dirs) and requires per-language detection
  heuristics that drift from upstream. Listing every supported
  language in `project.yml` is essentially free at startup because
  Serena spawns the underlying language servers **lazily, on first
  tool call** — an unused language costs nothing beyond a few lines
  of YAML. The cost we *do* pay is dependency surface: any LSP
  Serena bundles is now installable on the runner. That is acceptable
  because (a) `setup-uv` already caches the wheel set, (b) Serena's
  upstream packaging treats LSP installation as on-demand for most
  languages, and (c) `setup_serena.sh` stays fail-soft so a
  per-language LSP install failure degrades that language to the
  legacy `read`/`grep` fallback without blocking the run.
- **Written into the workspace root** (`$GITHUB_WORKSPACE/.serena/
  project.yml`).
- **Excluded from the implement.yml Guard 0 baseline diff** — the
  baseline snapshot in `${RUNTIME_DIR}/codex_pre_baseline.txt`
  (documented in `probably_unnecessary_but_read_if_stuck.md`) must
  list `.serena/` so codex-produced changes don't accidentally
  include a Serena cache file. Phase 1 amends the baseline-snapshot
  step's `git status --porcelain -uall` filter set.
- **Index cache** (`.serena/cache/`) lives under `.serena/` and is
  excluded by the same baseline filter.
- **`.gitignore` rule**: this repo's `.gitignore` already lists
  `.serena/` (line 6), so no Phase 1 edit is required here. The
  consumer-repo install guidance (§6.3) still recommends the same
  rule for repos that don't already have it, so a developer running
  interactively never accidentally commits the cache.

### 4.4 MCP wiring (extending `write_codex_config.sh`)

Two delivery options were considered:

- **A — extend `write_codex_config.sh`** to optionally append
  `[mcp_servers.serena]` based on `--serena-enabled`.
- **B — append the block in `setup_serena.sh` after `write_codex_
  config.sh` runs**.

**Choice: B.** Reasoning:

- `write_codex_config.sh` has a tight test contract
  (`tests/test_write_codex_config.py`) and 9 callers. Adding a
  conditional MCP-emission flag risks regressing the writer's main
  responsibility and forces every caller to grow a new flag.
- `setup_serena.sh` is a new script with no existing callers; it can
  own the MCP block emission cleanly.
- The append-after pattern is what the `[Unreleased]` CHANGELOG
  block already describes (it talks about "writing its `[mcp_servers.
  <name>]` block to `~/.codex/config.toml`" inside `setup_serena.sh`).

`setup_serena.sh` writes the block using Python with TOML-correct
string quoting. Pseudocode (illustrative — the real implementation in
Phase 1 must use TOML-valid string emission, *not* shell quoting):

```bash
if probe_mcp_handshake serena "$SERENA_BIN" "stdio"; then
    python3 - <<'PY' >> "$HOME/.codex/config.toml"
import os
# Prefer tomli_w when available so the output is parser-validated.
# Hand-emission fallback uses json.dumps for the command path: JSON
# basic strings are a strict subset of TOML basic strings (both use
# double quotes; both share \", \\, \n, \r, \t, \uXXXX escape rules
# for the bytes that need escaping at all). NOTE: shlex.quote is
# WRONG here — it shell-quotes (e.g. returns the bare string
# /usr/bin/serena-agent unquoted when no shell escaping is needed)
# and would emit invalid TOML.
import json
serena_bin = os.environ["SERENA_BIN"]
print()
print('[mcp_servers.serena]')
print(f'command = {json.dumps(serena_bin)}')
print('args = ["start-mcp-server", "--transport", "stdio"]')
print('startup_timeout_sec = 30')
PY
    echo "SERENA_AVAILABLE=true" >> "$GITHUB_ENV"
    echo "SERENA_PROBE target=setup result=ok" >&2
else
    echo "SERENA_AVAILABLE=false" >> "$GITHUB_ENV"
    echo "SERENA_PROBE target=setup result=failed reason=<probe-output>" >&2
fi
```

The real implementation in Phase 1 uses `tomli_w.dump()` round-tripped
against `tomllib.loads()` to validate output; the `json.dumps` path is
the no-`tomli_w` fallback. Either way, the regression in
`tests/test_setup_serena_toml.py` (added in Phase 1) parses the
produced `~/.codex/config.toml` with `tomllib` and asserts
`mcp_servers.serena.command` is a string equal to the input path —
catching any future regression to `shlex.quote` or other
shell-quoting accidents.

### 4.5 Reviewer carve-out (the strip-mcp invariant)

`scripts/review_run_reviewers.sh:193`-`200` strips every
`[mcp_servers.*]` table from the per-reviewer codex-home before each
reviewer pass:

```awk
/^[[:space:]]*\[mcp_servers\./ { skip=1; next }
```

This invariant **must be preserved**. Reasoning is in §3.3: reviewers
work on bounded diffs with Semble-prefetched context; they should not
spawn an exploration loop. The strip rule already handles future MCP
servers transparently — Serena gets stripped automatically. No edit
to `review_run_reviewers.sh` is needed for the carve-out.

A regression test in `tests/test_review_reviewer_strip_mcp.py` (new
in Phase 1) asserts that after `setup_serena.sh` writes the Serena
block, the per-reviewer codex-home file produced by
`review_run_reviewers.sh` contains no `[mcp_servers.serena]` table.

### 4.6 Lazy bootstrap gating

Per `analysis/workflow-optimization-2026-05-01-2.md` recommendation
"Skip Serena/bootstrap setup on flows that cannot reach code-editing":

For each in-scope workflow, the `setup_serena.sh` step is placed
*after* every cheap gate that can short-circuit execution:

| Workflow | Gates that must run BEFORE setup_serena |
|---|---|
| `implement.yml` | Label gate, "no actionable plan" exit |
| `validate.yml` | "no harness candidate" exit |
| `review_autofix.yml` | Comment-only path detection, post-merge no-op exit, no-linked-issue exit |
| `conflict-resolver` (inside `review_autofix.yml`) | "no conflict" exit |

The gate-to-setup ordering is enforced by step order in the workflow
YAML. A workflow-log-analysis assertion (added in Phase 1, Phase 5
verification) flags any run where `setup_serena.sh` ran but the job
exited within 30s without a `codex exec` step — that pattern is the
fingerprint of a gate ordering regression.

### 4.7 Sandbox / cleanup compatibility

- `write_codex_config.sh` writes `--sandbox danger-full-access` for
  GHA runs. Under that sandbox, codex shells out without restriction;
  Serena (running as an MCP child process spawned by codex) inherits
  the same surface. No additional sandbox gymnastics required.
- `implement.yml`'s Guard 0 baseline-diff filter (snapshot before the
  retry loop) must list `.serena/` so Serena's runtime files don't
  count as Codex-produced changes — same pattern as the existing
  `.codex-workflow-src*` exclusion (see
  `probably_unnecessary_but_read_if_stuck.md`).
- End-of-job stats collection runs **before** the cleanup step that
  removes `.serena/`. The cleanup step is moved (where present) or
  given a `if: always()` predecessor that copies stats out of the
  runtime tree first. This addresses the "review run logged 'No
  Serena tool usage stats found'" gap from
  `analysis/workflow-optimization-2026-05-02-3.md`.

### 4.8 Prefix-cache compatibility

The static prefix produced by `build_static_context.sh` is unchanged.
Serena's presence is conveyed only through:

1. The `[mcp_servers.serena]` block in `~/.codex/config.toml` (not
   part of the prompt).
2. The `{{SERENA_TOOL_HINTS}}` placeholder rendered into the
   *dynamic* context layer (per-issue; never cached).

Prefix-cache hit rate is unaffected.

### 4.9 Composition with Semble

Both Serena and Semble live behind opt-in flags (`SERENA_ENABLED` /
`SEMBLE_ENABLED`). They compose without conflict:

- Semble runs in the *prompt-assembly* phase (before `codex exec`).
  Its output is static text in the dynamic context.
- Serena runs in the *codex-exec* phase. Its tools are exposed to the
  model via MCP and only fire when the model invokes them.

A run with both enabled emits `SEMBLE_QUERY` lines during prompt
assembly and `SERENA_QUERY` lines during codex execution. They share
no state and have no ordering dependency.

---

## 5. Phased rollout

Five phases. Each phase is independently shippable, leaves the
pipeline in a working state, and is observable before the next phase
merges. No phase removes a legacy path; phases 1–4 are additive only.
Phase 5 is the consumer-repo propagation.

### 5.1 Phase 1 — Plumbing only

**Lands**: install + probe + config-writer extension, default-off
flag, log-prefix contract.

- Add `scripts/setup_serena.sh`. Responsibilities: install pinned
  Serena via uv, generate `.serena/project.yml` from a template
  (`scripts/templates/serena_project.yml.j2` — see below; the
  template enables every Serena-supported language by default per
  §4.3, so no per-repo language detection is required), run handshake
  probe, on success append `[mcp_servers.serena]` to
  `~/.codex/config.toml`, export `SERENA_AVAILABLE` to
  `$GITHUB_ENV`. Fail-soft.
- Add `scripts/mcp_handshake_probe.py`. Responsibilities: spawn a
  named MCP server, send a JSON-RPC `initialize` request, read the
  response, validate id match + `result.serverInfo` presence, exit
  0/1. Configurable timeout via `MCP_HANDSHAKE_PROBE_TIMEOUT` (env,
  default 15s); kill switch via `MCP_HANDSHAKE_PROBE_ENABLED` (env,
  default `true`). Reusable for Serena, Context7, Git MCP.
- Add `scripts/templates/serena_project.yml.j2`. Static shape across
  all consumer repos: enable every Serena-supported language (§4.3),
  exclude `.git/`, `node_modules/`, `dist/`, `.venv/`,
  `__pycache__/`, `.serena/cache/`. The template has no per-repo
  variables today; it's a `.j2` file only to leave room for future
  per-repo overrides (e.g. extra exclude globs) without renaming.
- `.gitignore` already lists `.serena/` (line 6 of this repo's
  `.gitignore`). No edit required here. Phase 5 release notes
  recommend the same line for consumer repos that don't already have
  it.
- Add `tests/test_mcp_handshake_probe.py` with cases: success,
  timeout, EOF mid-handshake, spawn failure, invalid JSON, error
  response, id mismatch. Plus a bash-level test that
  `setup_serena.sh` writes the block iff probe passes and respects
  `MCP_HANDSHAKE_PROBE_ENABLED=false` (forces success).
- Add `tests/test_setup_serena_toml.py`: parses the produced
  `~/.codex/config.toml` with `tomllib` and asserts
  `mcp_servers.serena.command` is a string equal to the input
  binary path. Catches regressions to shell-style quoting (the
  `shlex.quote` accident the §4.4 pseudocode warns against).
- Add fixtures under `tests/fixtures/mcp_handshake/`:
  `mock_mcp_close_on_init.py`, `mock_mcp_invalid_json.py`,
  `mock_mcp_id_mismatch.py`, `mock_mcp_happy.py`.
- Add a "Setup Serena" step to `.github/workflows/implement.yml`
  only (smallest blast radius). Step is gated on `vars.SERENA_ENABLED
  == 'true'` (default `false`). Step runs *after* the existing label
  gate / no-actionable-plan exit (lazy bootstrap, §4.6).
- Extend `implement.yml`'s Guard 0 baseline-diff filter to exclude
  `.serena/`.
- Add `SERENA_QUERY`, `SERENA_FALLBACK`, and `SERENA_PROBE` to
  `agents.md` "Stable log prefixes (contractual)" — append-only
  per `CLAUDE.md` §6.
- Add a section to `probably_unnecessary_but_read_if_stuck.md`
  describing the Serena bootstrap, probe, project.yml lifecycle,
  and stats-preservation contract.

**Acceptance**: with `SERENA_ENABLED=true` set on a single test
issue, the implement-phase log shows the probe succeeded, the
`[mcp_servers.serena]` block is present in the runtime
`~/.codex/config.toml` (`cat`-able from a debug step), and codex
finishes without reporting MCP-related errors. With `SERENA_ENABLED=
false` (default), prompt diff and `~/.codex/config.toml` content are
byte-identical to the pre-merge baseline.

### 5.2 Phase 2 — `implement` prompt + tool hints

**Lands**: §3.1 — implement editor sees Serena tools and the prompt
hints them.

- Add `{{SERENA_TOOL_HINTS}}` placeholder handling to
  `scripts/render_prompt.sh` alongside the existing
  `{{WORKFLOW_EDIT_RESTRICTION}}` machinery. Empty-string substitution
  when `SERENA_AVAILABLE != true`.
- Edit `prompts/mode-implement.txt` to include `{{SERENA_TOOL_HINTS}}`
  in a clearly-labelled subsection. Hint content lists the Serena
  tools and emphasises the cost contrast with `read`/`grep`.
- Add a regression test in `tests/test_implement_prompt_shape.py`
  asserting the placeholder is present iff `SERENA_AVAILABLE=true`.
- Wire the implement-phase caller in `.github/workflows/implement.yml`
  to export `SERENA_AVAILABLE` from `$GITHUB_ENV` into the
  `render_prompt.sh` invocation environment.

**Acceptance**: one real implement run (smoke) on a small issue with
`SERENA_ENABLED=true` shows Serena tool calls in the codex log
(`SERENA_QUERY target=implement tool=find_symbol …` lines) and a
non-empty Serena tool-call count in the end-of-job stats. With
`SERENA_ENABLED=false`, behaviour is byte-identical to today.

### 5.3 Phase 3 — `implement-diagnose` / `implement-repair` (+`-syntax`) and `review_autofix` editor

**Lands**: §3.2 + §3.3 (editor pass only; reviewer carve-out preserved).

- Add `{{SERENA_TOOL_HINTS}}` placeholder to
  `prompts/mode-implement-diagnose.txt`,
  `prompts/mode-implement-repair.txt`,
  `prompts/mode-implement-repair-syntax.txt`.
- Verify the in-place config patch in `implement.yml:1869` (repair
  reasoning override) preserves the `[mcp_servers.serena]` block.
  Adjust the patch if it currently rewrites the file (it should
  edit one line only).
- Add `setup_serena.sh` step + Guard 0 filter extension to
  `.github/workflows/review_autofix.yml`, gated on `SERENA_ENABLED`
  + comment-only / no-linked-issue gates (lazy bootstrap, §4.6).
- Edit `scripts/review_apply_fixes.sh` to render `{{SERENA_TOOL_HINTS}}`
  into the editor prompt.
- Add `tests/test_review_reviewer_strip_mcp.py` asserting that the
  reviewer carve-out (§4.5) still strips `[mcp_servers.serena]`.
- Update `tests/test_review_apply_fixes_prompt_shape.py` (or
  equivalent) to assert the editor prompt has the placeholder.

**Acceptance**: one real `review_autofix` autofix run shows
`SERENA_QUERY target=review-autofix-editor pr=<num>` lines in the
editor pass, and zero `[mcp_servers.serena]` registrations in the
per-reviewer logs.

### 5.4 Phase 4 — `validate` + conflict resolver

**Lands**: §3.4 + §3.5.

- Add `setup_serena.sh` step + Guard 0 filter + lazy gating to
  `.github/workflows/validate.yml`.
- Add `{{SERENA_TOOL_HINTS}}` placeholder to all four
  `prompts/mode-validate-*.txt` files.
- Add `{{SERENA_TOOL_HINTS_RESOLVER}}` placeholder (read-only Serena
  tools menu) to `prompts/conflict-resolver.txt`,
  `prompts/integration-sync-conflict-resolver.txt`,
  `prompts/integration-sync-conflict-resolver-retry-prelude.txt`.
- Edit `scripts/review_conflict_resolve.sh` to render the
  resolver-scoped placeholder. (The conflict resolver runs inside
  the `review_autofix.yml` job set up in Phase 3, so no new workflow
  step is needed.)
- Update Phase 4 prompt-shape tests accordingly.

**Acceptance**: one real validate self-heal run and one real
conflict-resolver run succeed with `SERENA_QUERY` lines visible in
their respective logs.

### 5.5 Phase 5 — Consumer-repo propagation via `@stable` release

**Status**: operational; not executed by this repo-only plan.

**Lands**: a new `@stable` release that ships the Serena bootstrap
through the *reusable workflows* under `.github/workflows/`. Per
`docs/semble-integration-plan.md` §5.5: consumer-side
`workflow-templates/*.yml` wrappers are NOT edited (they use `uses:`
and cannot also have `steps:`).

- Confirm phases 1–4 have added the "Setup Serena" step to every
  in-scope reusable workflow under `.github/workflows/`:
  - `implement.yml`
  - `review_autofix.yml`
  - `validate.yml`
- Cut a `@stable` release. The release workflow's repository-dispatch
  step (governed by `.github/ai/consumer_repos.json` per `CLAUDE.md`
  §14) notifies every entry. Consumer wrappers do not need to change
  because they pin to `@stable`; their next phase invocation
  resolves the updated reusable workflow at runtime.
- Smoke: run an end-to-end issue on one consumer repo with
  `SERENA_ENABLED=true` set as a repo var.

**Acceptance**: one consumer-repo issue produces a PR via the new
path; `cost_audit.py` shows reduced editor input tokens on that
issue relative to a comparable historical issue on the same repo,
and `SERENA_QUERY` log prefixes appear in the consumer-repo workflow
log.

---

## 6. Consumer-repo distribution (reusable workflows + `@stable`)

### 6.1 Reusable-workflow surface

The consumer-side wrappers under `workflow-templates/*.yml` are
reusable-workflow callers — each job uses `uses:
shubhodeep1/coding-workflows/.github/workflows/<phase>.yml@stable`
and `secrets: inherit`. A job using `uses:` cannot also have a
`steps:` sequence (GHA schema), so the Serena setup step **must not**
be added to the templates. It goes in the **reusable workflows**
under `.github/workflows/` that the templates call.

Reusable workflows that need the Serena setup step (in-scope per
Q1):

1. `implement.yml`
2. `review_autofix.yml`
3. `validate.yml`

The internal wrappers (`internal-*.yml`) are thin callers to the
reusable workflows above; they do not duplicate the setup step
locally.

### 6.2 SERENA_ENABLED flag

A new repo-var `SERENA_ENABLED` (default `false`) gates everything.
The flag is read by the `setup_serena.sh` step in each reusable
workflow and exported into `$GITHUB_ENV` for downstream scripts.
Consumer repos opt in by setting the repo-var explicitly on their
own repository — the reusable workflow inherits the *caller* repo's
vars when invoked via `uses:`. The default-false posture means
consumer repos that pick up the new `@stable` reusable workflow but
haven't opted in stay on the legacy path.

### 6.3 Wrapper-pin policy interaction

Consumer-repo wrappers pin to `@stable` (see `CLAUDE.md` §14 +
`wrapper pin policy` in the operator runbook). Phase 5 cuts a new
`@stable` tag *after* phases 1–4 have soaked on this repo. Because
the consumer wrappers themselves are unchanged, the propagation
requires only the new tag — the next reusable-workflow invocation
resolves to the updated `.github/workflows/<phase>.yml`. No per-repo
manual edits are required.

Consumers should add `.serena/` to their `.gitignore` independently
(this can't be propagated automatically). Phase 5's release notes
include a one-line consumer-repo checklist:

```
- [ ] Add `.serena/` to .gitignore (Serena writes a runtime cache there).
- [ ] Set `SERENA_ENABLED=true` as a repo var to opt in.
```

### 6.4 GH_PAT scope

Per `CLAUDE.md` §14: the `GH_PAT` used in the release workflow must
have `repo` scope on every listed consumer repo for the dispatch to
succeed. This plan adds no new consumer repos, so no PAT change is
required.

### 6.5 Consumer-repo language coverage (all-languages-on default)

`.serena/project.yml` is generated at runtime by `setup_serena.sh`
from the `serena_project.yml.j2` template (§5.1). The template
enables **every Serena-supported language by default** — Python,
TypeScript / JavaScript, Go, Rust, Java, C#, Ruby, PHP, C / C++,
Kotlin, Swift, plus any future additions Serena ships. The same
template covers every consumer repo regardless of language mix.

Rationale (full discussion in §4.3): consumer repos vary widely
(Python-only, TS-only, Python + TS + Go in one repo, polyglot
monorepos, scripting-language repos with build steps in another
language). A file-extension census at bootstrap is fragile. Listing
every supported language up-front is essentially free because Serena
spawns the underlying language servers **lazily on first tool call** —
unused languages cost only a handful of YAML lines.

Failure modes:

- A language server for one of the listed languages fails to install
  on a particular runner. `setup_serena.sh` is fail-soft per-language
  where Serena supports that granularity; otherwise the whole Serena
  surface flips to `SERENA_AVAILABLE=false` and the run continues on
  the legacy `read`/`grep` path. Either way the workflow does not
  block. The `SERENA_PROBE` log line records the per-language failure
  for diagnostic purposes.
- A pure-config repo (no Serena-supported source files) bootstraps
  Serena successfully but the model never invokes Serena tools. No
  failure, no fallback log line — just a `serena_query_count=0`
  reading in `cost_audit.py` for that run, which is correct.
- A language Serena does not support (e.g. a brand-new language not
  yet in upstream). The model treats it as it does today (codex's
  built-in `read`/`grep`); Serena tools simply return "no symbols
  found" for that file's queries.

This default-on posture means the operator does not have to maintain
a per-consumer-repo language profile, and adding a new language to a
consumer repo (or to Serena upstream) is automatically picked up the
next time `setup_serena.sh` runs.

---

## 7. Risks, rollback, backward compatibility

### 7.1 Risk surface

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Serena install fails in GHA (uv/network blip) | low | low | `setup_serena.sh` is fail-soft → `SERENA_AVAILABLE=false` → all callers fall back to legacy grep+read. |
| MCP handshake probe times out on a slow runner | low | low | Probe timeout configurable via `MCP_HANDSHAKE_PROBE_TIMEOUT` (default 15s); on timeout the block is omitted, codex starts without Serena, model uses legacy tools. |
| `[mcp_servers.serena]` block triggers OpenRouter Azure HTTP 400 (the failure mode the probe is meant to catch) | low | high pre-mitigation, very-low post-mitigation | The probe **is** the mitigation. If the failure shape ever evolves past what the probe catches, the workflow-log-analysis pipeline flags persistent post-Serena Azure 400s and the operator can flip `SERENA_ENABLED=false` repo-var-side. |
| LSP server crashes mid-edit | low | medium | Serena's MCP layer surfaces the crash to codex as a tool-error response; the model falls back to `read`/`grep` for the rest of the loop. Stats collection captures crash count per phase via a new `SERENA_FALLBACK reason=lsp-crash` line. |
| `.serena/` cache files leak into a Codex-produced diff and trip Guard 0 / Guard 1 | medium | medium | Phase 1 baseline-snapshot filter excludes `.serena/`; same pattern as `.codex-workflow-src*`. Regression test in `tests/test_implement_baseline_filter.py` (added in Phase 1). |
| Reviewer pass accidentally inherits Serena (carve-out regression) | low | medium | The strip-mcp invariant in `review_run_reviewers.sh:193-200` handles this transparently. `tests/test_review_reviewer_strip_mcp.py` (added in Phase 3) asserts the carve-out continues to fire after the Serena block is added. |
| Lazy bootstrap regression — Serena setup runs on a short-circuit path | medium | low | Workflow-log-analysis assertion (Phase 1) flags any `setup_serena.sh` run on a job that exited within 30s without `codex exec`. Surfaces as a `WORKFLOW_AUDIT` line in the periodic audit. |
| Stats lost to cancel/cleanup (the gap from `analysis/workflow-optimization-2026-05-02-3.md`) | medium | low | End-of-job stats step uses `if: always()` and runs *before* the `.serena/` cleanup step. Regression test in `tests/test_review_stats_preservation.py`. |
| Static-prefix cache inadvertently breaks | very low | high | Phase 1 explicitly tests that `build_static_context.sh` output is byte-identical pre/post merge. CI assertion added. |
| `apply_patch_tool_type` regression reopened | very low | high | This plan does not touch the editor config beyond appending an MCP block. `tests/test_write_codex_config.py` continues to enforce the `apply_patch_tool_type` setting (the MCP append happens *after* the writer, not inside it; the writer's TOML output is unchanged). |
| Codex sandbox accidentally widens trust by exposing Serena tools to the model | n/a | n/a | This is the *intended* behaviour — Serena's tools are designed for the model to call. The `--sandbox danger-full-access` posture is unchanged from today; Serena does not expand the surface beyond what apply_patch already grants. |
| Consumer-repo wrapper drift (template updated, scripts not) | low | medium | Phases 1–4 land scripts before phase 5 cuts the release. The release dispatcher only fires once phases 1–4 are merged + tagged. |

### 7.2 Rollback

Per phase:

- **Phases 1–4**: revert the merging PR. Default `SERENA_ENABLED=
  false` means the legacy path was always the live path until the
  operator flipped the var; flipping it back is the runtime
  rollback. The PR revert is the structural rollback. No data
  migration is involved.
- **Phase 5** (consumer-repo propagation): cut a new `@stable` tag
  with the reusable workflows reverted to their pre-Serena shape;
  the next dispatch flows the rollback to consumers. Consumer repos
  that already opted in via repo-var stay opted-in but their
  reusable workflows no longer install or register Serena, which
  their downstream scripts handle as `SERENA_AVAILABLE=false` —
  identical to the never-opted-in path.

### 7.3 Backward-compatibility audit (`CLAUDE.md` §6)

No identifiers are renamed. Specifically:

- `SERENA_QUERY`, `SERENA_FALLBACK`, `SERENA_PROBE` are *new* log
  prefixes, added alongside the existing contractual prefixes — no
  existing prefix is touched. `agents.md` "Stable log prefixes"
  list is **append-only**.
- `SERENA_ENABLED` / `SERENA_AVAILABLE` /
  `MCP_HANDSHAKE_PROBE_ENABLED` / `MCP_HANDSHAKE_PROBE_TIMEOUT` are
  *new* env vars; defaults are configured so absent vars behave
  identically to the pre-plan pipeline.
- `scripts/setup_serena.sh`, `scripts/mcp_handshake_probe.py`,
  `scripts/templates/serena_project.yml.j2`, the
  `{{SERENA_TOOL_HINTS}}` and `{{SERENA_TOOL_HINTS_RESOLVER}}`
  placeholders are all new artefacts — no rename collisions with
  existing files.
- The reviewer strip-mcp behaviour in `scripts/review_run_reviewers.
  sh` is unchanged; this plan relies on it but does not modify it.
- `write_codex_config.sh` is unchanged; the Serena MCP block is
  appended after that script runs, not edited into its emit path.
- Section numbers in this document are referenced from `agents.md`
  / no other places yet, but follow the §6 numbering-immutability
  rule preemptively.

### 7.4 MongoDB / DB-contract considerations (`CLAUDE.md` §10)

This plan does not touch any MongoDB collection or write path, so
§10 does not apply.

---

## 8. Observability and measurement

### 8.1 New log prefixes (contractual)

- `SERENA_PROBE target=<setup|context7|git|...> result=<ok|failed|skipped> [reason=<...>]`
  emitted by `mcp_handshake_probe.py` for each MCP server it probes.
  Reused for Context7 and Git MCP per the `[Unreleased]` CHANGELOG
  block.
- `SERENA_QUERY target=<phase> tool=<tool-name> [symbol=<name>] ms=<t> response_bytes=<m>`
  emitted on every Serena tool call (parsed out of codex's tool-call
  log by an end-of-job summariser script — `scripts/serena_stats_
  emit.py`, new in Phase 1).
- `SERENA_FALLBACK target=<phase> reason=<probe-failure|setup-failure|disabled|lsp-crash>`
  emitted whenever a phase that *would* have used Serena uses the
  legacy path instead.

All three prefixes are added to `agents.md` "Stable log prefixes
(contractual)" in Phase 1.

**Stream**: all three go to **stderr** (or as `::notice::` /
`::warning::` workflow commands), never to stdout. Codex's tool-call
log already lives in stderr-equivalent space; the summariser script
emits its rollup lines to stderr.

### 8.2 Cost audit integration

Extend `scripts/cost_audit.py` with new buckets:

- `serena_query_count` per workflow (total Serena tool calls).
- `serena_query_bytes` per workflow (sum of response_bytes for
  Serena calls).
- `serena_fallbacks` per workflow.
- `serena_targets[target]` — target-scoped breakdown (call count /
  byte count / fallback count).
- Estimated token-savings comparison: `editor_input_tokens_actual`
  vs `editor_input_tokens_legacy_estimate` (the latter computed by
  multiplying Serena query count by an empirical "tokens per
  read+grep loop iteration" constant calibrated against pre-Serena
  baselines).

The periodic workflow-log-analysis (`prompts/mode-workflow-
analysis.txt`) flags any workflow whose `serena_fallbacks /
serena_query_count` ratio exceeds a configurable threshold over a
rolling window — that's the signal Serena is broken in some
structural way and the plan's "assume fallback handles it" assumption
is no longer holding.

### 8.3 Stats preservation through cancellation / cleanup

The "review run logged 'No Serena tool usage stats found'" gap
called out in `analysis/workflow-optimization-2026-05-02-3.md` is
addressed by:

1. End-of-job stats step uses `if: always()` so cancellation /
   timeout / failure paths still fire it.
2. The step runs *before* any cleanup that removes `.serena/`
   (Phase 1 reorders the cleanup to run last).
3. The summariser reads from a Serena-managed stats file (path TBD
   by Serena upstream; if unavailable, a fallback parser scans the
   codex log for tool-call markers).
4. Rollup lines are emitted to stderr so they survive log capture
   even when the runner is being torn down.

A regression test (`tests/test_review_stats_preservation.py`,
Phase 3) injects a synthetic cancel and asserts the stats lines are
present in the captured log.

### 8.4 Smoke matrix

Phase 5 acceptance includes running the new path against:

- One Python-heavy consumer repo.
- One JavaScript/TypeScript-heavy consumer repo.
- This repo (`shubhodeep1/coding-workflows`) itself, via a self-
  issue that exercises the validate-self-heal path.

Three repos cover the language-mix Serena's LSP layer has to
handle.

### 8.5 Cost / A-B measurement plan (Q5F)

To validate the ~85% input-token-reduction projection from
`analysis/workflow-optimization-2026-05-02-3.md` on real runs:

1. **Baseline window**: collect editor input-token totals from the
   last 30 days of `implement` runs (pre-Serena). Compute median +
   p95 per repo.
2. **Treatment window**: after Phase 2 lands, with `SERENA_ENABLED=
   true` on a single test issue, collect the same metric for 10
   consecutive runs.
3. **Target**: median input-token reduction ≥ 60% (conservative
   target; the analysis projection is 85%, but ablation noise is
   significant).
4. **Kill criteria**: if the treatment-window median input-token
   *increase* exceeds 10% (e.g. Serena's per-tool-call overhead
   dominates the read savings on small issues), pause Phase 3
   rollout and either retune the prompt hint (de-emphasising
   Serena for small edits) or scope Serena to large-issue paths
   only.
5. **Long-run measurement**: `cost_audit.py` keeps the new buckets
   indefinitely; the periodic workflow-log-analysis surfaces the
   30-day rolling delta in the audit report.

---

## 9. Open questions

These are decisions that need to be made during phases 1–2, not
preconditions for starting:

- **Q9.1**: Pin Serena to a specific version, or float on `latest`?
  Likely answer: pin (the unattended pipeline values reproducibility).
  First pin TBD on first-cut PR after sampling upstream stable
  releases.
- **Q9.2**: For the prompt hint section, do we list the Serena tool
  *signatures* (so the model can call them more confidently) or
  just the tool *names* (relying on codex's MCP-served tool schemas
  to teach the model)? Default to names-only (the schemas are
  authoritative); revisit after Phase 2 if the model under-uses
  Serena.
- **Q9.3**: `setup_serena.sh` writes the `[mcp_servers.serena]`
  block by appending to `~/.codex/config.toml`. If a future MCP
  server (Context7, Git) writes its block via a different append
  path, do we need a coordinator to avoid TOML duplication? Not
  today (the `[Unreleased]` CHANGELOG block describes the same
  append pattern for Context7 and Git, presumably in the same
  `setup_serena.sh`). If Context7 / Git move out of that script,
  factor out the append helper.
- **Q9.4**: For the validate phase: do `discover` and `diagnose`
  benefit enough from Serena to justify the per-pass tool registration?
  Default to "yes — register, let the model decide whether to call".
  Revisit after Phase 4 if `serena_query_count` is consistently 0
  for those passes.
- **Q9.5**: Smoke-matrix consumer-repo selection (§8.4) — concrete
  picks once `.github/ai/consumer_repos.json` is consulted at the
  time of Phase 5 cut. The current registry has 11 entries; the
  Phase 5 PR should record the chosen pair plus rationale.
- **Q9.6**: For the resolver-scoped placeholder
  (`{{SERENA_TOOL_HINTS_RESOLVER}}`, §3.5), do we enforce the
  read-only restriction at the prompt level (text-only) or at the
  MCP level (don't expose the write tools)? Default to prompt-level
  (simpler, no Serena config gymnastics); revisit if the model
  ever calls `replace_symbol_body` from a resolver path.

These are *Q-IDs from this document*, not pipeline-clarification
Q-IDs. Defer answering until the relevant phase.

---

## 10. Future work (out of scope for this plan)

- **Interactive Claude Code adoption**. Excluded by Q3. Could be
  proposed separately once the unattended path has soaked. Likely
  shape: a `.mcp.json` stanza or settings block that wires Serena
  for interactive sessions in this repo.
- **Replacing Semble** with Serena's `find_symbol`-keyed pre-fetch.
  Tempting but premature; Semble's CLI-based pre-fetch is cheaper
  per call than the MCP round-trip Serena would require. Reconsider
  if Semble's accuracy degrades on a specific phase.
- **Retiring `targeted_file_context.py` whole-file inlining** in
  favour of Serena symbol-level context. The targeted-inlining path
  is cheaper and more accurate for plan-named files; keep it. Same
  reasoning as the semble-plan §10.
- **Cross-repo Serena queries** (orchestrator looks up patterns in
  `coding-workflows` while editing a consumer repo). Plausibly
  useful; would need a multi-project Serena setup. Out of scope
  here.
- **Serena's `write_memory` / `read_memory` tools** — could replace
  parts of `scripts/ai_memory.py` for codex-side state. Substantial
  surface; defer to a separate plan once Serena core is soaked.
- **Re-using the LSP index across jobs** via `actions/cache`. Same
  trade-off as semble — index build is fast and freshness matters.
  Revisit if LSP startup ever dominates a phase's wall time.

---

## 11. Summary checklist

For the implementer (future me) — the bare-minimum work to ship this
plan, ordered by dependency:

- [ ] Phase 1 — `scripts/setup_serena.sh`, `scripts/mcp_handshake_
      probe.py`, `scripts/templates/serena_project.yml.j2` (all
      Serena-supported languages enabled by default per §4.3 / §6.5),
      `tests/test_mcp_handshake_probe.py` + fixtures,
      `tests/test_setup_serena_toml.py` (TOML-validity regression for
      the `mcp_servers.serena.command` quoting), `agents.md`
      log-prefix table addition, `implement.yml` Setup Serena step
      (lazy-bootstrapped) + Guard 0 baseline filter extension,
      `probably_unnecessary_but_read_if_stuck.md` Serena section.
      `.gitignore` already lists `.serena/` (line 6); no edit
      required in this repo.
- [ ] Phase 2 — `{{SERENA_TOOL_HINTS}}` placeholder in
      `scripts/render_prompt.sh`, `prompts/mode-implement.txt` hint
      block, regression test `tests/test_implement_prompt_shape.py`,
      `implement.yml` env-pass to render_prompt.sh, smoke run with
      `SERENA_ENABLED=true`.
- [ ] Phase 3 — `{{SERENA_TOOL_HINTS}}` in
      `prompts/mode-implement-diagnose.txt`,
      `prompts/mode-implement-repair.txt`,
      `prompts/mode-implement-repair-syntax.txt`; Setup Serena step
      + Guard 0 + lazy gating in `review_autofix.yml`;
      `scripts/review_apply_fixes.sh` placeholder render;
      `tests/test_review_reviewer_strip_mcp.py` carve-out test;
      stats-preservation test
      `tests/test_review_stats_preservation.py`.
- [ ] Phase 4 — Setup Serena step in `validate.yml`; `{{SERENA_
      TOOL_HINTS}}` in all four `prompts/mode-validate-*.txt`;
      `{{SERENA_TOOL_HINTS_RESOLVER}}` in conflict-resolver prompts;
      `scripts/review_conflict_resolve.sh` placeholder render;
      Phase 4 prompt-shape tests.
- [ ] Phase 5 — Confirm Setup Serena step is in every relevant
      reusable workflow under `.github/workflows/`. Cut `@stable`.
      Smoke on one consumer repo. **Operational release step; not
      executed by this repo-only plan.**
- [ ] Cost audit extension (`scripts/cost_audit.py`) — Serena
      telemetry buckets + target breakdown + fallback ratio.
- [ ] Workflow log analysis prompt
      (`prompts/mode-workflow-analysis.txt`) updated to flag high
      `SERENA_FALLBACK` ratios and lazy-bootstrap regressions.
- [ ] Reconciliation of `CHANGELOG.md [Unreleased]` Serena
      entries (§12).

---

## 12. Appendix — CHANGELOG / analysis-docs reconciliation (Q5G)

### 12.1 Aspirational entries to reconcile

The current `CHANGELOG.md [Unreleased]` block contains:

> Pre-flight MCP handshake probe (`scripts/mcp_handshake_probe.py`)
> plus `probe_mcp_handshake` helper in `scripts/setup_serena.sh`.
> For each enabled optional MCP server (Context7, Git), the setup
> script now performs a JSON-RPC `initialize` exchange before
> writing its `[mcp_servers.<name>]` block to
> `~/.codex/config.toml`. Servers that fail the probe (timeout, EOF
> mid-handshake, malformed/error response, id mismatch) are
> omitted [...]

and:

> Serena MCP integration across all workflows

Neither of those reflects the state of *this* branch. Two options:

- **Option A — delete the entries** from `[Unreleased]` on this
  branch. They will be re-added by the Phase 1 PR when the work
  actually lands.
- **Option B — leave them** as documentation of the intended end
  state, and let the Phase 1 PR mark them as "kept; now reflects
  reality".

**Recommended**: Option A. The CHANGELOG is supposed to describe
*what shipped*, not what's planned. Aspirational entries in
`[Unreleased]` confuse readers who diff the changelog against
`main`. Phase 1 of this plan is the right place to re-introduce
them with the actual implementation details.

### 12.2 Analysis doc references

Several `analysis/workflow-optimization-2026-05-0*.md` files quote
Serena measurements (269 tool calls, 94% efficiency, ~26k vs ~181k
tokens) as if from real runs. On this branch those measurements are
not reproducible — Serena is not wired here. Two interpretations:

- The analysis docs were written against a fork or a future-merge
  branch where Serena had landed.
- The numbers are projections / upstream-borrowed and the analysis
  docs should mark them as such.

**Recommended**: when Phase 2 produces the first real Serena
measurements (per §8.5), append a short reconciliation note to the
relevant analysis doc(s) — not a rewrite, just a "validated by
run #<id> on <date>: actual reduction was <n>%" footer. This keeps
the historical analysis intact while grounding the numbers.

### 12.3 `test-and-mark-stable.yml` comments

`.github/workflows/test-and-mark-stable.yml` contains two comments
that mention "Serena/MCP breakage" as a regression class to watch
for. Those comments are accurate and stay as-is — they describe a
risk pattern the smoke test is supposed to catch, not the presence
of an integration.

### 12.4 No prior Serena artefacts to preserve

A repo-wide grep for `serena` (case-insensitive) across `scripts/`,
`prompts/`, `.github/workflows/`, `workflow-templates/` returns:

- Two comments in `.github/workflows/test-and-mark-stable.yml`
  (described in §12.3 — keep).
- Zero references in `scripts/`, `prompts/`,
  `workflow-templates/`.

So Phase 1 can create `scripts/setup_serena.sh`,
`scripts/mcp_handshake_probe.py`, etc., without any rename / merge
considerations from prior Serena code.

---

## Sign-off

This plan is design-only. No code in this repo changes as part of
this commit. The next concrete step is Phase 1: creating
`scripts/setup_serena.sh`, the handshake probe, the project-file
template, the `implement.yml` plumbing, and the
`agents.md` log-prefix entries. Phase 1 should land as a single PR
with the test additions and a debug step that `cat`s
`~/.codex/config.toml` so reviewers can verify the
`[mcp_servers.serena]` block is shaped correctly.
