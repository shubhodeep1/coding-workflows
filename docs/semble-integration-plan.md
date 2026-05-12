# Plan: Semble Integration Across the Unattended Pipeline

> **Status**: PARTIALLY IMPLEMENTED IN-REPO — Semble helpers, overflow support,
> telemetry, and reusable-workflow wiring have landed on this repo; consumer
> rollout still requires a separate operational `@stable` tag cut.
> **Owner**: solo developer (sole user of the workflows in this repo).
> **Goal**: replace every grep+read code-context-fetch site in the unattended
> codex-cli pipeline with Semble (semantic-chunk retrieval) invoked as a CLI
> from shell scripts, and propagate the same change to consumer repos through
> the reusable workflows they call. `workflow-templates/` remain thin caller
> wrappers; they are not the install/index surface.
> **Scope decisions (locked)**:
>
> - **Q1 — Surface**: every grep+read context-fetch site in `scripts/` and
>   `prompts/` plus consumer-repo propagation through reusable workflows
>   consumed by `workflow-templates/` callers.
> - **Q2 — Track**: unattended pipeline only. Interactive Claude Code sessions
>   are out of scope for this plan; they may adopt Semble later under a
>   separate proposal.
> - **Q3 — Delivery**: CLI invocation from shell scripts. No MCP server. The
>   codex-cli editor stays on `apply_patch` (function-typed) per the
>   2026-05-07 ablation outcome documented in `agents.md`.

This plan deliberately stays inside the existing pipeline architecture —
prompt prefix caching, `targeted_file_context.py` inlining, codex-cli with
`apply_patch` — and adds Semble as an *additional* context-fetch path, not
a replacement for the file-inlining strategy. Removing whole-file inlining
is explicitly out of scope (that was the rejected option D in Q1).

---

## 1. Background

### 1.1 How code context is fetched today

The unattended pipeline assembles prompt context in three layers:

1. **Static prefix** (`scripts/build_static_context.sh`) — system instructions
   + agents.md + phase-trimmed `ai_pipeline.md` + README trim. Identical
   across runs of a given phase, so OpenRouter prefix-caches it.
2. **Targeted file inlining** (`scripts/targeted_file_context.py`) — likely-
   to-edit files pasted verbatim into the dynamic-context section, bounded
   at 100 KB total. Files that overflow get a marker:
   `"(would overflow total budget — read with read tool)"` so codex falls
   back to its built-in `read` tool.
3. **Codex-driven exploration at runtime** — for anything the static and
   targeted layers did not cover, codex uses its built-in `read` and
   shell-`grep` tools, paying full token cost for every line surfaced.

Layer 3 is the integration target for this plan. Layers 1 and 2 stay
unchanged in shape; layer 2 grows a Semble-backed overflow path.

### 1.2 Where layer 3 actually fires

Inventory of grep+read context-fetch sites in the unattended pipeline:

| Site | Phase(s) | What it fetches today |
|---|---|---|
| `prompts/mode-implement.txt` parallel-reads / verify-after-write | implement | Files referenced by the plan but not inlined; symbol-level lookups for cross-file edits. |
| `prompts/mode-judge.txt`, `prompts/mode-orchestrate-poll-judge.txt` | judge / orchestrate-poll | Wave artefacts the judge prompt explicitly says to "read with read tool". |
| `prompts/mode-validate-discover.txt`, `mode-validate-diagnose.txt`, `mode-validate-self-heal.txt` | validate | Harness state, suspected failure-site files, log fragments. |
| `prompts/mode-implement-diagnose.txt`, `mode-implement-repair.txt`, `mode-implement-repair-syntax.txt` | implement-diagnose / implement-repair | Files implicated by the codex post-failure trace. |
| `prompts/conflict-resolver.txt`, `integration-sync-conflict-resolver.txt`, `integration-sync-conflict-resolver-retry-prelude.txt` | conflict resolver | Both sides of the conflict + nearby callers. |
| `prompts/mode-judge-review-blocked.txt`, `mode-judge-stall-recovery.txt` | judge sub-modes | Issue + linked PR state; cross-issue context. |
| `scripts/review_run_reviewers.sh` reviewer-prompt assembly | review autofix | Per-reviewer context bundle. |
| `scripts/review_conflict_resolve.sh`, `review_conflict_prepare.sh` | conflict resolver | Conflict-marker neighbourhoods. |
| `scripts/self_heal_validation.sh` | validate | Failing-test source + nearby files. |
| `scripts/review_apply_fixes.sh`, `review_consolidate.sh` | review autofix | Files referenced by reviewer comments. |
| `scripts/implement_diagnose_post_codex_failure.sh` | implement-diagnose | Files in the codex failure tail. |
| `scripts/validate_driver.sh` | validate | Discovery scans for harness candidates. |

### 1.3 Why Semble fits this layer

- **CLI-only invocation** matches the existing context-builder pattern (a
  shell script runs a Python helper, captures stdout, appends to a prompt
  file). No new wiring on the codex-cli editor path.
- **~250 ms cold-index, ~1.5 ms query** on CPU — survives ephemeral GHA
  runners without infra changes.
- **Zero network, zero API key, zero GPU** — preserves the no-external-
  dependency property of the inner loop.
- **Output is text chunks** — appendable to the dynamic-context section
  without disturbing prefix-cache hit rate (only the dynamic section
  varies per run).

### 1.4 Why not the rejected alternatives

- **MCP server** (Q3 option B): would require rewiring the codex-cli editor.
  The 2026-05-07 ablation pinned editor reliability to
  `apply_patch_tool_type: function` for OpenAI slugs (see `agents.md`
  models table footnote); inserting an MCP toolbelt risks reopening that
  failure mode.
- **Replace file-inlining entirely** (Q1 option D): the targeted-inlining
  path is cheaper and more accurate than chunk retrieval for phases where
  the plan already names files-to-touch. Keep it; let Semble cover the
  overflow + exploration cases only.
- **Interactive Claude Code only** (Q2 option B): Claude Code already has
  Read/Edit/Grep + Agent for exploration. Token cost on the unattended
  side is the actual pain.

---

## 2. Goal and non-goals

### 2.1 Goal

For every grep+read context-fetch site listed in §1.2, provide a Semble
query path that:

1. Runs as a local CLI inside the GHA job (no network, no daemon).
2. Emits a bounded text block compatible with the existing dynamic-
   context shape (header + `--- BEGIN/END CHUNK ---` markers).
3. Falls back to the legacy grep+read path if Semble fails for any
   reason (binary missing, index error, query timeout) — the pipeline
   must never deadlock on Semble unavailability.
4. Is propagated to consumer repos through the reusable workflows under
   `.github/workflows/`; consumer-side `workflow-templates/*.yml` stay as
   thin callers pinned to `@stable`, so uptake still depends on a separate
   `@stable` release dispatch to every entry in
   `.github/ai/consumer_repos.json` (currently 11 entries, one of which
   is `shubhodeep1/coding-workflows` itself for the self-dispatch path —
   so 10 external consumer repos plus this repo).

### 2.2 Non-goals

- Not removing whole-file inlining (Q1 option D rejected).
- Not introducing an MCP server (Q3 option B rejected).
- Not changing `apply_patch_tool_type`, model slugs, reasoning effort,
  or any element of the editor configuration.
- Not changing the prompt prefix cache shape (`build_static_context.sh`
  output stays byte-identical for the static prefix).
- Not extending Semble to interactive Claude Code (out of scope per Q2).
- Not replacing log-parsing `grep` calls (those are not code-context
  fetches; they read CI log output, where Semble is irrelevant).

### 2.3 Acceptance criteria

The plan is "implemented" when, on the unattended pipeline:

1. Every site in §1.2 either calls Semble or has a documented reason for
   not doing so.
2. Token-cost telemetry (cost_audit.py) shows a measurable reduction in
   per-issue editor input tokens on at least three real issues across at
   least two consumer repos.
3. The Semble path's failure rate (Semble-error → legacy-fallback events)
   is observable in workflow logs via a new `SEMBLE_FALLBACK` log prefix
   (added to the contractual prefix list in `agents.md` §"Stable log
   prefixes").
4. Consumer-repo propagation is in place: at least one `@stable` release
   has published the updated reusable workflows, consumer repos have
   resolved that tag via their existing thin wrappers, and a follow-up
   issue has been processed end-to-end on a consumer repo using the new
   path.

---

## 3. Integration surface (catalog)

This catalog is the working list for the rollout. Each entry names the
existing fetch shape, the proposed Semble call shape, and the fallback
trigger.

### 3.1 `scripts/targeted_file_context.py` — overflow path

**Today**: files that would push the cumulative byte budget past 100 KB
emit `(would overflow total budget — read with read tool)`.

**With Semble**: the overflow marker is replaced with a bounded chunk-
retrieval block keyed off the plan's task summary. New CLI flags:

```
--semble-bin <path>          Path to the semble binary. Resolved at
                              runtime via `shutil.which("semble")` when
                              not supplied; the helper exits 0 with a
                              `SEMBLE_FALLBACK` log line if the binary
                              cannot be found on PATH.
--semble-index <dir>          Index directory (default:
                              ${RUNTIME_DIR}/.semble-index)
--semble-query-from <file>    File whose contents become the query (e.g., the
                              plan's "Goal" section)
--semble-max-chunks <n>       Hard cap on chunks per overflowed file (default 6)
--semble-fallback {marker,read,off}
                              On Semble failure: emit the legacy marker
                              ("marker"), inline a head of the file via the
                              existing read path ("read"), or skip
                              ("off"). Default: marker.
```

Output shape inside the dynamic context block:

```
--- FILE: <rel> (<bytes> bytes — chunk-retrieved via semble) ---
<chunk 1 with line-range comment>
...
--- END FILE: <rel> ---
```

**Fallback**: any non-zero exit from the Semble CLI → emit the legacy
marker and continue. Log `SEMBLE_FALLBACK target=overflow file=<rel> reason=<exit-code-or-stderr-tail>`.

### 3.2 Reviewer pass — `scripts/review_run_reviewers.sh`

**Today**: each reviewer prompt receives the diff plus a fixed pre-bundle
of "files referenced by the diff" gathered via grep over the changed-files
list.

**With Semble**: after the diff bundle, append a Semble query keyed on the
PR title + each reviewer's role-prompt summary. Cap at 12 chunks total
(reviewer pass already has a higher token budget).

**Fallback**: on Semble error, fall through to the existing pre-bundle
behaviour. Log `SEMBLE_FALLBACK target=reviewer pr=<num> reviewer=<role>`.

### 3.3 Conflict resolver — `scripts/review_conflict_resolve.sh` and the conflict-resolver prompts

**Today**: the conflict-resolver prompt receives the conflicted hunks
plus a `targeted_file_context.py --paths-file <conflicted-files>` block.

**With Semble**: in addition to the targeted block, run a Semble query for
each conflicted symbol's name (extracted from the hunk via simple regex —
existing code in `review_conflict_prepare.sh` already does this for the
"affected symbols" log line). Cap 4 chunks per symbol, max 16 total.

**Fallback**: if symbol extraction fails or Semble errors, the existing
targeted block alone is sufficient — the resolver already works without
the new query. Log `SEMBLE_FALLBACK target=conflict-resolver pr=<num>`.

### 3.4 Validate phases — `scripts/self_heal_validation.sh`, `validate_driver.sh`, and `mode-validate-*` prompts

**Today**: the validate phases scan candidate harness files via grep over
known patterns (`describe(`, `def test_`, etc.) and then `read` the top
matches into the prompt.

**With Semble**: after the structural-pattern scan, run a Semble query for
the failing-assertion text (when discoverable from the run log) and for
the changed-file symbol names. Cap 8 chunks total.

**Fallback**: structural-pattern scan output is already sufficient. Log
`SEMBLE_FALLBACK target=validate phase=<discover|diagnose|self-heal>`.

### 3.5 Implement-diagnose / implement-repair — `scripts/implement_diagnose_post_codex_failure.sh` and friends

**Today**: the diagnose path reads the codex failure tail and the
plan-named files; the repair path reads the files codex partially edited.

**With Semble**: query for the failure-tail's identifier set (function
names and file basenames extracted via regex). Cap 6 chunks. The repair
path adds a query for the partially-edited files' symbol set.

**Fallback**: legacy plan-files-only path. Log
`SEMBLE_FALLBACK target=implement-diagnose` /
`SEMBLE_FALLBACK target=implement-repair`.

### 3.6 Judge / orchestrate-poll-judge — `prompts/mode-judge.txt`, `mode-orchestrate-poll-judge.txt`

**Today**: the judge prompt instructs the model to "read with read tool"
when wave artefacts need inspection.

**With Semble**: prepend a small "Semble pre-fetch" section to the judge
prompt that ran a fixed query (`"wave failure" OR "blocking issue"
OR "stall"`) over the wave-relevant subset of files. Cap 4 chunks. The
existing `read tool` instruction stays — Semble is additive, not a
replacement.

**Fallback**: the existing `read tool` instruction is the fallback. Log
`SEMBLE_FALLBACK target=judge wave=<id>`.

### 3.7 Sites that stay grep-only

These sites are *not* code-context fetches; Semble is not a fit:

- `scripts/render_prompt.sh` — placeholder substitution (literal grep on
  template tokens).
- `prompts/mode-validate-generate.txt` — embedded shell that grep-checks
  CI log output for SIGTERM / `BINARY_AUDIT_OK`. Log-stream grep, not
  code search.
- `prompts/mode-workflow-api-redundancy.txt` mentions of `grep` — refers
  to log-key conventions, not a literal call.
- All `gh_helpers.sh` / `label_helpers.sh` / `memory_helpers.sh` grep —
  they parse CLI output, not source code.

These will be skipped explicitly with a one-line comment in the rollout
PR so future readers see the decision was deliberate.

---

## 4. Architecture

### 4.1 Component layout

```
+----------------------------------------------------------------+
| GHA job (codex-cli phase, e.g. .github/workflows/implement.yml)|
|                                                                |
|  1. actions/checkout (fetch-depth: 0)                          |
|  2. setup uv  (new — see install path below) .............    |
|  3. install_semble.sh (current repo state: pip-based,         |
|     fail-soft; uv is staged for future installer migration)   |
|  4. semble index . --out ${RUNTIME_DIR}/.semble-index ...      |
|     ^- ~250 ms; index lives in $RUNTIME_DIR for the job        |
|                                                                |
|  5. build_static_context.sh <phase> <static.txt>               |
|  6. targeted_file_context.py ... --semble-bin $(which semble) \|
|         --semble-index ${RUNTIME_DIR}/.semble-index ...        |
|  7. (phase-specific reviewer / resolver / validate scripts     |
|      shell out to `semble query` directly)                     |
|  8. codex exec ... < <(cat static.txt dynamic.txt)             |
+----------------------------------------------------------------+
```

### 4.2 Install path

Current repo state: the workflows already stage `setup-uv`, but the
shared installer remains `scripts/install_semble.sh`, which is currently
pip-based and fail-soft. The extra `setup-uv` step landed as rollout
plumbing; it keeps the workflows ready for a future installer migration
without changing the present installer contract in this issue.

A new helper `scripts/install_semble.sh` encapsulates:

- Pinned version (e.g. `semble==<X.Y.Z>` — exact pin TBD on first stable
  cut; see §9 open questions).
- Version detection on `semble --version` for the installer's pin check.
- Idempotent: skip install if `which semble` resolves and reports the
  pinned version.
- Fail-soft: on install failure, set `SEMBLE_AVAILABLE=false` in
  `$GITHUB_ENV` and exit 0. Every Semble caller respects this flag and
  falls back without the per-site failure-log noise.

### 4.3 Index lifecycle

- **One index per job, scoped to the workspace.** Index path:
  `${RUNTIME_DIR}/.semble-index` (matches the existing `RUNTIME_DIR`
  convention used by `build_static_context.sh`).
- **Build the index once**, immediately after checkout, before any phase
  step. New step `Build semble index` in each `ai-*.yml` workflow.
- **Skip if `SEMBLE_AVAILABLE=false`** — sets `SEMBLE_INDEX_AVAILABLE=false`
  and downstream callers fall back to legacy paths.
- **No cross-job caching.** `actions/cache` is not used for the index
  itself. The index is faster to rebuild (~250 ms on indexed corpora of
  the size we operate on) than the cache restore round-trip; caching
  also creates a staleness risk vs. the just-checked-out tree.

### 4.4 Query envelope

A small shared bash function `semble_query_block` lives in a new
`scripts/semble_helpers.sh`:

```
semble_query_block <query-text> <max-chunks> <header-label> [<extra-flags>...]
```

It:

1. Bails out if `SEMBLE_INDEX_AVAILABLE != true`. Caller fallback runs.
2. Runs `semble query "<text>" --index "${RUNTIME_DIR}/.semble-index"
   --top-k <max-chunks> --format text`.
3. Wraps stdout in `=== SEMBLE: <header-label> ===` / `=== END SEMBLE
   ===` markers so the prompt text is clearly delimited and easy to grep
   out for debugging.
4. On non-zero exit, returns 1 to signal the caller to run its fallback
   path.

**Stream discipline (mandatory)**: callers redirect `semble_query_block`
stdout into the prompt file. Therefore stdout is reserved exclusively
for the prompt chunk block (the marker-wrapped Semble output, or empty
on bail-out). All log lines — `SEMBLE_QUERY target=<…> chunks=<n>
bytes=<m> ms=<t>` on success and `SEMBLE_FALLBACK target=<…>
[exit=<code>] reason=<…>` on failure or bail-out — go to **stderr**,
which the caller redirects to the GHA job log (typically already
captured by the runner). Equivalently, the helper may emit the log
lines as `::notice::` / `::warning::` workflow commands, which GHA
routes to the log without touching stdout.

A regression test in `tests/` (added in phase 1) asserts that
`SEMBLE_QUERY` / `SEMBLE_FALLBACK` strings never appear in the prompt
file produced by `semble_query_block` — that assertion is the
contractual guard against the contamination Copilot raised in PR
review.

This keeps every per-site integration to ~3 lines: build the query,
call `semble_query_block` (stdout → prompt; stderr → job log), on
failure run the legacy path.

### 4.5 Sandbox compatibility

`scripts/write_codex_config.sh` writes `--sandbox danger-full-access` for
GHA runs (lines 14–22 of that file document the rationale: workspace-
write apply_patch hangs on the v0.113+ codex). Under
`danger-full-access`, codex itself can also shell out to `semble` if any
prompt paths add a "you may run `semble query …` directly" instruction.
This plan does **not** instruct codex to call Semble itself — every
Semble call is made by shell scripts *before* `codex exec`, so Semble
output enters the prompt as static text and codex's tool surface stays
unchanged. (This is the same pattern as `targeted_file_context.py`.)

### 4.6 Prefix cache compatibility

The static prefix produced by `build_static_context.sh` is unchanged.
Semble output is always emitted into the **dynamic** context layer (the
per-issue file passed to codex after the static prefix). Prefix-cache
hit rate is unaffected.

---

## 5. Phased rollout

Five phases. Each phase is independently shippable, leaves the pipeline
in a working state, and is observable before the next phase merges. No
phase removes a legacy path; phases 1–4 are additive only. Phase 5 is
the consumer-repo propagation.

### 5.1 Phase 1 — Plumbing only

**Status**: landed in-repo.

**Lands**: install helper, helpers script, initial workflow plumbing.

- Add `scripts/install_semble.sh`.
- Add `scripts/semble_helpers.sh` with `semble_query_block`.
- Add a "Build semble index" step to `.github/workflows/implement.yml`
  only (smallest blast radius — `implement.yml` is the reusable
  workflow that the consumer-side `workflow-templates/ai-implement.yml`
  wrapper calls via `uses: ...@stable`). Step is gated on
  `vars.SEMBLE_ENABLED == 'true'` (default false).
- Add `SEMBLE_QUERY` and `SEMBLE_FALLBACK` to the stable-log-prefix list in
  `agents.md` § "Stable log prefixes (contractual)".
- Add a section to `probably_unnecessary_but_read_if_stuck.md` describing
  the index lifecycle and fallback behaviour.

**Acceptance**: with `SEMBLE_ENABLED=true` set on a single test issue,
the implement-phase log shows `semble index` succeeded and that no
downstream phase changed behaviour (prompt diff = empty).

### 5.2 Phase 2 — `targeted_file_context.py` overflow path

**Status**: landed in-repo.

**Lands**: the overflow path uses Semble.

- Implement `--semble-bin / --semble-index / --semble-query-from /
  --semble-max-chunks / --semble-fallback` flags in
  `scripts/targeted_file_context.py`.
- Wire the implement-phase caller (`render_prompt.sh` invocation in
  `.github/workflows/implement.yml`) to pass the new flags.
- Add a unit test in `tests/` for the overflow → Semble emission shape
  (mock `semble` binary).

**Acceptance**: a synthetic test plan whose targeted files exceed 100 KB
produces a `--- FILE: ... — chunk-retrieved via semble ---` block in the
prompt; flipping `SEMBLE_ENABLED=false` reverts to the legacy marker
without other diffs.

### 5.3 Phase 3 — Reviewer + conflict resolver

**Status**: landed in-repo.

**Lands**: §3.2 and §3.3.

- Edit `scripts/review_run_reviewers.sh` to call `semble_query_block`
  after the existing pre-bundle.
- Edit `scripts/review_conflict_resolve.sh` and
  `scripts/review_conflict_prepare.sh` to call `semble_query_block` for
  each affected symbol.
- Extend `tests/` reviewer-prompt-shape tests to assert the new Semble
  block is present iff `SEMBLE_ENABLED=true`.

**Acceptance**: one real PR autofix run (smoke) shows the new Semble
block in the reviewer prompt logs and the autofix succeeds end-to-end.

### 5.4 Phase 4 — Validate + implement-diagnose + judge

**Status**: landed in-repo.

**Lands**: §3.4, §3.5, §3.6.

- Edit `scripts/self_heal_validation.sh`, `scripts/validate_driver.sh`,
  `scripts/implement_diagnose_post_codex_failure.sh` to call
  `semble_query_block`.
- Add Semble pre-fetch sections to `prompts/mode-judge.txt` and
  `prompts/mode-orchestrate-poll-judge.txt` (the prompts get a
  templated `{{SEMBLE_PREFETCH}}` placeholder filled by
  `render_prompt.sh`; the existing `read tool` instruction stays).
- Add `{{SEMBLE_PREFETCH}}` placeholder handling to `render_prompt.sh`
  alongside the existing `{{WORKFLOW_EDIT_RESTRICTION}}` machinery.

**Acceptance**: one real validate self-heal run and one stall-recovery
judge run succeed with the new blocks visible in the prompt logs.

### 5.5 Phase 5 — Consumer-repo propagation via `@stable` release

**Status**: not performed in this repo-only issue; operational follow-up.

**Lands**: a new `@stable` release that ships the install + index steps
through the *reusable workflows* under `.github/workflows/`. The
consumer-side `workflow-templates/*.yml` wrappers are NOT edited — they
are reusable-workflow callers (`uses: shubhodeep1/coding-workflows/.github/workflows/<phase>.yml@stable`)
and a job using `uses:` cannot also have `steps:` (GHA schema). The
templates already pin to `@stable`, so cutting the new tag is what
flows the change to consumers transitively.

- Confirm phases 1–4 have added the `Install semble` + `Build semble
  index` steps to every relevant reusable workflow under
  `.github/workflows/`:
  - `implement.yml`
  - `review_autofix.yml`
  - `validate.yml`
  - `orchestrate.yml`, `orchestrate_poll.yml`,
    `orchestrate_clarify_respond.yml`
  - `clarify.yml`, `plan.yml`
  - Internal wrappers (`internal-*.yml`) remain thin callers to the
    reusable workflows above; they do not duplicate the install/index
    steps locally.
- Cut a `@stable` release. The release workflow's repository-dispatch
  step (governed by `.github/ai/consumer_repos.json` per CLAUDE.md §14)
  notifies every entry (currently 11 — 10 external consumer repos plus
  this repo's self-dispatch path). Consumer wrappers do not need to
  change because they already pin to `@stable`; their next phase
  invocation resolves the updated reusable workflow at runtime.
- Smoke: run an end-to-end issue on one consumer repo
  (e.g. `shubhodeep1/mongo-explorer`) with `SEMBLE_ENABLED=true` set as
  a repo var.

**Acceptance**: one consumer repo's issue produces a PR via the new
path; cost_audit.py shows reduced editor input tokens on that issue
relative to a comparable historical issue on the same repo.

**Issue-scope note**: this document now reflects landed in-repo work, but
the actual tag cut / repository-dispatch fanout remains a separate
operator action and is intentionally out of scope for this repo-only
documentation + observability follow-up.

---

## 6. Consumer-repo distribution (reusable workflows + `@stable`)

### 6.1 Reusable-workflow surface

Status on this repo: the reusable-workflow side has already been wired in
for the in-repo rollout. Consumer propagation is therefore primarily a
release-channel problem now, not a template-editing problem.

The consumer-side wrappers under `workflow-templates/*.yml` are
reusable-workflow callers — each job uses
`uses: shubhodeep1/coding-workflows/.github/workflows/<phase>.yml@stable`
and `secrets: inherit`. A job using `uses:` cannot also have a `steps:`
sequence (GHA schema), so install/index steps **must not** be added to
the templates. They go in the **reusable workflows** under
`.github/workflows/` that the templates call.

The reusable workflows that run a codex-cli phase need:

1. A `setup uv` step (already present on the current in-repo rollout).
2. A new `Install semble` step (calls `scripts/install_semble.sh`, which
   is currently pip-based and fail-soft).
3. A new `Build semble index` step.
4. The downstream phase steps already shell out to scripts that, after
   phases 1–4 land, internally use Semble; no per-workflow change
   beyond adding 1–3.

### 6.2 SEMBLE_ENABLED flag

A new repo-var `SEMBLE_ENABLED` (default `false`) gates everything. The
flag is read by the install/index steps in the reusable workflows and
exported into `$GITHUB_ENV` for downstream scripts. Consumer repos opt
in by setting the repo-var explicitly on their own repository — the
reusable workflow inherits the *caller* repo's vars when invoked via
`uses:`. The default-false posture means consumer repos that pick up
the new `@stable` reusable workflow but haven't opted in stay on the
legacy path.

### 6.3 Wrapper-pin policy interaction

Consumer-repo wrappers pin to `@stable` (see CLAUDE.md §14 +
`wrapper pin policy` in the operator runbook). Phase 5 cuts a new
`@stable` tag *after* phases 1–4 have soaked on this repo. Because the
consumer wrappers themselves are unchanged, the propagation requires
only the new tag — the next reusable-workflow invocation resolves to
the updated `.github/workflows/<phase>.yml`. No per-repo manual edits
are required.

### 6.4 GH_PAT scope

Per CLAUDE.md §14: the `GH_PAT` used in the release workflow must have
`repo` scope on every listed consumer repo for the dispatch to succeed.
This plan adds no new consumer repos, so no PAT change is required.

---

## 7. Risks, rollback, backward compatibility

### 7.1 Risk surface

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Semble install fails in GHA (network blip, registry hiccup) | medium | low | `install_semble.sh` is fail-soft → `SEMBLE_AVAILABLE=false` → all callers fall back. |
| Semble index build fails on a weird repo (binary blobs, submodules) | low | low | Index step catches non-zero exit, sets `SEMBLE_INDEX_AVAILABLE=false`. Same fallback path. |
| Semble query timeout slows down a phase | low | medium | `semble query` is wrapped in a `timeout 5s` guard inside `semble_query_block`. On timeout → fallback. |
| Chunk output bloats the dynamic prompt past the model's context window | medium | medium | Per-call hard cap on chunk count. `cost_audit.py` is extended to record Semble byte contribution per phase so regressions show up in the periodic audit. |
| Static-prefix cache inadvertently breaks | very low | high | Phase 1 explicitly tests that `build_static_context.sh` output is byte-identical pre/post merge. CI assertion added. |
| Consumer-repo wrapper drift (template updated, scripts not) | low | medium | Phases 1–4 land scripts before phase 5 cuts the release. The release dispatcher only fires once phases 1–4 are merged + tagged. |
| `apply_patch_tool_type` regression reopened | very low | high | This plan does not touch the editor config. A CI assertion (already present per `tests/test_write_codex_config.py`) continues to enforce the freeform/function setting. |
| Codex sandbox accidentally widens trust by exposing `semble` to the model | very low | medium | Semble is invoked *before* `codex exec`, in the wrapping shell script. Codex itself never calls `semble`. The prompt does not mention Semble's CLI surface — the model only sees the resulting text block. |

### 7.2 Rollback

Per phase:

- **Phases 1–4**: revert the merging PR. Default `SEMBLE_ENABLED=false`
  means the legacy path was always the live path until the operator
  flipped the var; flipping it back is the runtime rollback. The PR
  revert is the structural rollback.
- **Phase 5** (consumer-repo propagation): cut a new `@stable` tag with
  the templates reverted to their pre-Semble shape; the next dispatch
  flows the rollback to consumers. Consumer repos that already opted in
  via repo-var stay opted-in but their wrappers no longer install or
  index, which their scripts handle as `SEMBLE_AVAILABLE=false` —
  identical to the never-opted-in path.

### 7.3 Backward-compatibility audit (CLAUDE.md §6)

No identifiers are renamed. Specifically:

- `SEMBLE_FALLBACK` is a *new* log prefix, added alongside the existing
  contractual prefixes — no existing prefix is touched.
- `SEMBLE_ENABLED` / `SEMBLE_AVAILABLE` / `SEMBLE_INDEX_AVAILABLE` are
  *new* env vars; defaults are configured so absent vars behave
  identically to the pre-plan pipeline.
- New CLI flags on `targeted_file_context.py` are all optional;
  existing callers that omit them get pre-plan behaviour byte-for-byte.
- `scripts/install_semble.sh`, `scripts/semble_helpers.sh`, and the
  `{{SEMBLE_PREFETCH}}` placeholder are all new artefacts — no rename
  collisions with existing files.
- `agents.md` "Stable log prefixes" list is **append-only** — section
  numbers and existing entries unchanged (CLAUDE.md §6 covers this).

### 7.4 MongoDB / DB-contract considerations (CLAUDE.md §10)

This plan does not touch any MongoDB collection or write path, so §10
does not apply. The `_locks` index-creation discipline and the index-
registry rules are unaffected.

---

## 8. Observability and measurement

### 8.1 New log prefixes (contractual)

- `SEMBLE_FALLBACK target=<site> [pr=<num>] reason=<...>`
  emitted whenever a Semble call returns non-zero or times out and the
  caller switches to its legacy path.
- `SEMBLE_QUERY target=<site> chunks=<n> bytes=<m> ms=<t>` emitted on
  every successful Semble call. Cheap, machine-parseable, lets the
  workflow-log-analysis phase track Semble's actual contribution.

Both prefixes are added to `agents.md` "Stable log prefixes
(contractual)".

**Stream**: both prefixes go to **stderr** (or as `::notice::` /
`::warning::` workflow commands), never to stdout. Stdout is reserved
for prompt content. See §4.4 for the per-call helper contract and the
phase-1 regression test that enforces this.

### 8.2 Cost audit integration

Extend `scripts/cost_audit.py` with two new buckets:

- `semble_query_bytes` / `semble_fallbacks` totals per workflow.
- `semble_targets[target]` — target-scoped query-call / logged-byte /
  fallback breakdown derived from `target=` fields, since current emitters
  do not expose a universal `phase=` field.

The periodic workflow log analysis (`prompts/mode-workflow-analysis.txt`)
should flag any workflow/target whose Semble fallback rate is persistently
high over a rolling window — that's the signal that Semble is broken in
some structural way and the plan's "assume fallback handles it"
assumption is no longer holding.

### 8.3 Smoke matrix

Phase 5 acceptance includes running the new path against:

- One Python-heavy consumer repo (e.g. `shubhodeep1/mongo-explorer`).
- One JavaScript/TypeScript-heavy consumer repo (e.g.
  `shubhodeep1/atlas-bridge.gd` or `shubhodeep1/binance-blessings` —
  TBD on first cut).
- This repo (`shubhodeep1/coding-workflows`) itself, via a self-issue
  that exercises the validate-self-heal path.

Three repos cover the language-mix Semble has to handle (its accuracy
varies by language per the upstream evaluation table).

---

## 9. Open questions

These are decisions that need to be made during phases 1–2, not
preconditions for starting:

- **Q9.1**: Pin Semble to a specific version, or float on `latest`?
  Likely answer: pin, since the unattended pipeline values
  reproducibility over keeping up with upstream. First pin TBD.
- **Q9.2**: For the implement-phase Semble query, what text do we feed
  it? Options: the plan's "Goal" section, the issue title + body, the
  files-likely-to-change list. Defaulting to "Goal section" feels
  best (most semantic signal), but worth measuring against the others
  during phase 2.
- **Q9.3**: ~~Should the index live in `${RUNTIME_DIR}/.semble-index` or
  `.semble-index` at the repo root?~~ **Resolved**: `${RUNTIME_DIR}/.semble-index`
  (no workspace pollution; matches the existing `RUNTIME_DIR`
  convention). The §4.1 diagram and §4.4 query-envelope code paths use
  this location consistently.
- **Q9.4**: For phases 5's smoke matrix: do we also exercise a Go or
  Rust consumer repo? Currently no consumer repo in
  `consumer_repos.json` is Go/Rust. If the consumer set grows during
  rollout, fold that repo into the smoke list.

These are *Q-IDs from this document*, not pipeline-clarification
Q-IDs. Defer answering until the relevant phase.

---

## 10. Future work (out of scope for this plan)

- **Interactive Claude Code adoption**. Excluded by Q2. Could be
  proposed separately once the unattended path has soaked.
- **MCP server delivery**. Excluded by Q3. Reconsider only if the
  unattended editor migrates off codex-cli (e.g. the Codex CLI Runner
  Migration Plan in `docs/codex-runner-migration.md` results in a
  client that wants MCP).
- **Replacing whole-file inlining** with chunk retrieval everywhere
  (Q1 option D). Reconsider after at least three months of phase 4
  data shows Semble's accuracy is comparable to inlining on the
  phases where the plan already names files. The bar is high because
  the cost of regressing editor accuracy is much greater than the
  token savings.
- **Indexing across multiple repos** for cross-repo queries (e.g. the
  orchestrator looking up patterns from `coding-workflows` while
  editing a consumer repo). Plausibly useful for the orchestrate
  decompose phase. Would need a cache layer Semble doesn't ship with.
- **Re-using the index across jobs** via `actions/cache`. Skipped in
  this plan because index build is faster than cache restore, but if
  the index ever gets large (e.g. multi-repo), cache becomes worth
  revisiting.

---

## 11. Summary checklist

For the implementer (future me) — the bare-minimum work to ship this
plan, ordered by dependency:

- [x] Phase 1 — `scripts/install_semble.sh`, `scripts/semble_helpers.sh`,
      initial reusable-workflow plumbing, `agents.md` log-prefix table
      addition.
- [x] Phase 2 — `scripts/targeted_file_context.py` overflow path with
      Semble, `tests/` overflow shape test, wire into
      `.github/workflows/implement.yml`.
- [x] Phase 3 — Reviewer + conflict resolver scripts, prompt-shape test
      updates.
- [x] Phase 4 — Validate + implement-diagnose + judge phases,
      `{{SEMBLE_PREFETCH}}` placeholder + `render_prompt.sh` handling.
- [ ] Phase 5 — Confirm install/index steps are present in every
      relevant reusable workflow under `.github/workflows/` (NOT in
      `workflow-templates/`, which use `uses:` and cannot carry
      `steps:`). Cut `@stable`. Smoke on one consumer repo. **Operational
      release step; not executed by this repo-only issue.**
- [x] Cost audit extension (`scripts/cost_audit.py`) — Semble telemetry
      totals + target breakdown + fallback counter.
- [x] Workflow log analysis prompt updated to flag high fallback rates.
