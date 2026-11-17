# Symphony-Inspired Improvements to the Orchestrator

## Archived status

This file is the canonical completed-plan record for tracking issue `#3042`.

The closeout summary below reflects the shipped repository state re-audited on
2026-06-05 UTC. The historical plan text that follows is preserved for
context; where the original future-tense plan conflicts with the closeout audit
or the narrower landed contracts called out below, the closeout sections are
authoritative. No drift is left implicit in this archive.

## Closeout summary

- Current HEAD ships all twelve Symphony workstreams, but four landed narrower
  than the original draft: S1's strict renderer is always-on contract
  enforcement with no repo-visible `STRICT_PROMPT_RENDER` warn-only surface or
  render-receipt JSON; W1's workspace reuse is cache-backed only (no
  `WORKSPACE_BACKEND=branch` fallback or `cache_evicted_unexpectedly` event);
  W2 loads optional hook files from `.github/ai/workspace_hooks/...` but does
  not commit a placeholder hook tree; and U1's `.github/ai/WORKFLOW.md`
  overlay currently validates only `prompt_overrides[]`, not `limits.*` or
  `workspace.hooks_dir`.
- `README.md` and `agents.md` now document the shipped Symphony-era operator
  surfaces instead of the broader draft: repo vars for workspace reuse / stall
  guard / thread reuse / state snapshots, the prompt-overrides-only
  `WORKFLOW.md` contract, `.github/ai/concurrency_caps.yml`,
  `.github/ai/workspace_hooks/<phase>/<hook>.sh`, and the run-substate /
  state-snapshot schema surfaces.
- Because the remaining differences are explicit landed drift rather than
  missing runtime surfaces, this archive preserves the full historical plan
  below and marks the affected phases as `Complete with drift` instead of
  silently flattening plan-vs-repo differences.

## Evidence snapshot

- Strict rendering / stall / thread reuse / force-tick:
  `scripts/render_prompt.py`, `scripts/render_prompt.sh`, `prompts/contracts/`,
  `scripts/codex_stall_guard.sh`, `scripts/codex_thread_reuse.sh`,
  `scripts/orchestrate_force_tick.sh`, `.github/workflows/implement.yml`,
  `tests/test_render_prompt_foundation.py`,
  `tests/test_codex_stall_guard_scripts.py`,
  `tests/test_codex_thread_reuse_core.py`,
  `tests/test_orchestrate_force_tick.py`
- Workspace reuse / hooks / safety / ledger:
  `scripts/workspace_init.sh`, `scripts/run_workspace_hook.sh`,
  `scripts/workspace_safety_check.sh`, `scripts/ledger_emit_substate.sh`,
  `.github/workflows/workspace-cache-maintenance.yml`,
  `ai-memory/schemas/run_ledger_entry.v1.json`,
  `docs/scripts-pending-removal.md`, `tests/test_workspace_init.py`,
  `tests/test_workspace_cache_maintenance.py`,
  `tests/test_workspace_hooks.py`, `tests/test_workspace_safety_check.py`,
  `tests/test_run_substate_ledger.py`
- Orchestrator caps / overlay / blocker check / snapshot:
  `.github/ai/concurrency_caps.yml`, `scripts/orchestrate_lib.py`,
  `scripts/orchestrate_poll_process.sh`, `scripts/load_workflow_overlay.py`,
  `ai-memory/schemas/workflow_overlay.v1.json`, `scripts/blocker_check.py`,
  `scripts/build_state_snapshot.py`, `ai-memory/schemas/state_snapshot.v1.json`,
  `tests/test_orchestrator_concurrency_caps.py`,
  `tests/test_workflow_overlay_core.py`, `tests/test_blocker_check.py`,
  `tests/test_state_snapshot.py`
- Live archival gate context: `gh issue view 3042 --json body`

## Phase-by-phase shipped status

| Phase | Status | Shipped repo truth | Remaining drift / blocker |
|---|---|---|---|
| S1 — strict renderer | Complete with drift | `scripts/render_prompt.py` loads per-mode prompt contracts, `render_prompt.sh` remains the shim entrypoint, and the renderer rejects `missing_required`, `unknown_in_template`, and `forbidden_present`; regression coverage lives in the `tests/test_render_prompt_*.py` suite. | Current HEAD has no repo-visible `STRICT_PROMPT_RENDER` warn-only switch, and the planned render-receipt JSON sidecar did not ship. |
| S2 — event-idle stall guard | Complete | `scripts/codex_stall_guard.sh` ships observe-only vs kill mode, implement / review / validate / judge / resolver callsites wire it in, and the poller backstop is sized around the guard. Coverage lives in `tests/test_codex_stall_guard_scripts.py` and `tests/test_codex_stall_guard_poller.py`. | None. |
| S3 — force-tick on phase end | Complete | `scripts/orchestrate_force_tick.sh` dispatches the poller with an ai-memory cooldown record, and implement / review / validate / resolver paths use the helper instead of ad-hoc dispatch logic. Coverage lives in `tests/test_orchestrate_force_tick.py`. | None. |
| S4 — continuation-turn reuse | Complete | `scripts/codex_thread_reuse.sh` ships the resume probe / session capture flow, implement / validate / review apply-fixes / conflict resolver wire it behind `CODEX_THREAD_REUSE_ENABLED`, and the continuation prompt assets are staged with the workflows. Coverage lives in `tests/test_codex_thread_reuse_core.py` and `tests/test_codex_thread_reuse_review.py`. | None. |
| S5 — per-state concurrency caps | Complete | `.github/ai/concurrency_caps.yml`, `scripts/orchestrate_lib.py::load_concurrency_caps`, and the poller snapshot / dispatch checks enforce capped per-state fan-out while leaving in-flight runs untouched. Coverage lives in `tests/test_orchestrator_concurrency_caps.py`. | None. |
| W1 — per-issue workspace cache | Complete with drift | `scripts/workspace_init.sh` computes stable workspace keys, exports `CREATED_NOW`, and the implement / validate / review flows use cache-backed workspace reuse plus the nightly `workspace-cache-maintenance.yml` cleanup job. Coverage lives in `tests/test_workspace_init.py` and `tests/test_workspace_cache_maintenance.py`. | The planned `WORKSPACE_BACKEND=branch` fallback and `cache_evicted_unexpectedly` ledger path are not present on current HEAD; workspace reuse is cache-backed only. |
| W2 — workspace lifecycle hooks | Complete with drift | `scripts/run_workspace_hook.sh` executes `after_create`, `before_run`, `after_run`, and `before_remove` hooks from `.github/ai/workspace_hooks/<phase>/<hook>.sh`, with timeout and failure semantics enforced in code and tests. | Current HEAD loads optional consumer-authored hook files but does not commit the draft's placeholder `.github/ai/workspace_hooks/` tree, and the hooks directory is not currently overlay-configurable. |
| W3 — filesystem-safety invariants | Complete | `scripts/workspace_safety_check.sh` enforces the reuse-path realpath / pwd / key invariants only when workspace reuse is enabled, matching the shipped mergeable-before-W1 rollout shape. Coverage lives in `tests/test_workspace_safety_check.py`. | None. |
| W4 — run-attempt sub-state ledger | Complete | `scripts/ledger_emit_substate.sh` records `run_substate` metadata plus token totals, `ai-memory/schemas/run_ledger_entry.v1.json` accepts the additive metadata / event surfaces, and implement / review / validate / judge / resolver callsites emit the common lifecycle substates. Coverage lives in `tests/test_run_substate_ledger.py`. | None. |
| U1 — `WORKFLOW.md` overlay | Complete with drift | `scripts/load_workflow_overlay.py` and `ai-memory/schemas/workflow_overlay.v1.json` ship a strict `.github/ai/WORKFLOW.md` loader, workflows stage it early, and `scripts/render_prompt.py` applies matching prompt overrides. Coverage lives in `tests/test_workflow_overlay_core.py` and `tests/test_workflow_overlay_orchestrator.py`. | The shipped overlay surface is narrower than the original draft: it currently accepts only `schema_version` + `prompt_overrides[]`, not `limits.*` or `workspace.hooks_dir`. |
| U2 — blocker-aware runtime dispatch | Complete | `scripts/blocker_check.py` evaluates `dependency_edges` fail-open when metadata is absent, and the poller defers dispatch with `dispatch_deferred_blocker` when blockers remain open. Coverage lives in `tests/test_blocker_check.py`. | None. |
| U3 — per-tick state snapshot | Complete | `scripts/build_state_snapshot.py` builds `state.json` from poller exports plus ledger enrichment, `ai-memory/schemas/state_snapshot.v1.json` validates the payload, and `.github/workflows/orchestrate_poll.yml` uploads the `state-snapshot` artifact plus optional branch publication. Coverage lives in `tests/test_state_snapshot.py`. | None. |

## Archival gate resolution

1. The repo audit finds landed drift, but not a current-HEAD blocker large
   enough to make archival misleading once the status table above is preserved.
   The narrowed implementations are explicit in S1, W1, W2, and U1 rather than
   being hidden missing code paths.
2. `gh issue view 3042 --json body` on 2026-06-05 UTC still shows the
   `symphony-closeout-audit-archive` checkbox unchecked, so
   `scripts/lint_plan_archival_completeness.py` will require either (a) the
   tracking body to be refreshed before PR creation or (b) a non-empty PR-body
   `## De-scoped phases` section that names that single still-open closeout
   row. That is a PR-metadata requirement, not a repository-file blocker.

The historical future-tense proposal is preserved below for reference. Where it
conflicts with the closeout summary or the phase table above, the closeout
sections are authoritative.

## Historical plan

# Symphony-Inspired Improvements to the Orchestrator

## Summary

Adopt twelve flag-gated, fail-open mechanisms borrowed from OpenAI's Symphony
orchestration spec — strict prompt rendering, Codex-event stall detection,
force-tick on phase end, continuation-turn reuse, per-state concurrency caps,
per-issue workspace reuse with lifecycle hooks and filesystem-safety
invariants, run-attempt sub-state ledger events, a per-consumer policy overlay,
runtime blocker checks, and a per-tick state snapshot — to cut wasted LLM
tokens and wall-clock latency **without** giving up this repo's multi-reviewer
panel, Docker validation harness, or `ai-memory` retrieval stack.

This plan is a faithful conversion of the former `docs/symphony-inspired-improvements.md`
design doc into the implementation-plan format the unattended orchestrator
consumes. The original doc is retired in the same PR (see
[This PR's housekeeping](#this-prs-housekeeping)).

## Context

The design reference is OpenAI's Symphony orchestration spec
(`openai/symphony` SPEC.md, March 2026). Symphony is a deliberately small
daemon: a tracker poller, a per-issue workspace, a single multi-turn agent
session, and a strict in-repo prompt template. This repository is the opposite
— a thick GitHub Actions pipeline with a multi-reviewer panel, Docker-based
validation harness, an `ai-memory` retrieval layer, and named stall-recovery
actions. This plan borrows Symphony's **mechanisms** (thread reuse, event-based
stall, force-tick, strict templating, persistent workspace, lifecycle hooks)
without importing its **policy** (open-ended turn loop, single-agent trust
model, tracker-write delegation). The net effect on per-issue LLM spend is
**cost-down or cost-neutral**.

**Current-state baseline** (verified in `scripts/orchestrate_lib.py`,
`scripts/orchestrate_poll_process.sh`, `scripts/render_prompt.sh`,
`.github/workflows/orchestrate_poll.yml`, `.github/workflows/implement.yml`,
`.github/workflows/review_autofix.yml`, `prompts/mode-*.txt`):

1. **Prompt rendering** — `scripts/render_prompt.sh` expands only the
   `{{SERENA_EFFICIENCY_BLOCK_*}}` placeholders (from
   `prompts/serena-efficiency-block.txt`) and fails on an unresolved
   placeholder, but performs no general per-mode variable templating. Other
   substitution happens via workflow YAML heredocs and env passthrough, with no
   declared per-mode variable contract.
2. **Stall detection** — `scripts/orchestrate_lib.py::detect_stalls()` is
   phase-age based (`now - status_since_ts > STALL_THRESHOLD_*_MINUTES`, where
   `status_since_ts` is set when the orchestrator first *observes* a label
   change). A live-but-stuck Codex session burns tokens until the phase-age
   threshold (commonly 30–60 min) or the zombie-runs Actions-age filter trips.
3. **Cadence** — `cron: '*/5 * * * *'` on `internal-orchestrate-poll.yml`, plus
   one immediate `workflow_dispatch` from `scripts/review_conflict_resolve.sh`
   on resolver failure (`IS_INTEGRATION_SYNC=true`). Every other phase-end event
   waits up to 5 min for the next tick.
4. **Codex invocation** — implement, review-autofix, validation-self-heal, and
   conflict-resolver paths re-render the full mode prompt on each attempt;
   thread reuse is not used today. Codex CLI is pinned to `@openai/codex@v0.114.0`
   via `.github/actions/install-codex`.
5. **Concurrency** — wave-serialized per tracking issue, but uncapped per phase
   across tracking issues. Ten PRs stranded in `ai:review-blocked` run ten
   parallel autofix loops.
6. **Workspaces** — ephemeral, scoped by run/attempt
   (`/tmp/codex-implement-${GITHUB_RUN_ID}` etc.). Setup (clone, deps, harness)
   repeats on every retry.
7. **Run-attempt observability** — job logs plus `ai-memory`
   `runs/<run-id>/ledger/events.jsonl` events; no sub-state vocabulary, no
   per-tick snapshot. The ledger schema lives at
   `ai-memory/schemas/run_ledger_entry.v1.json`.
8. **Policy surface** — split across `CLAUDE.md`, `agents.md`,
   `unattended_system_instructions.md`, ~25 `prompts/*` files, workflow env
   vars, and per-repo `.ai/validate.yml`; no single in-repo overlay.

**Sibling / related plans.**

- `docs/plans/gstack-learnings-plan.md` Phase O ("skill modularity refactor of
  `prompts/`") **overlaps Phase S1** here (strict prompt rendering). The repo
  already documents the reconciliation: whichever lands first ships the shared
  templating engine and the other reuses it. See
  [Risks & Mitigations](#risks--mitigations).
- The existing force-tick precedent is `scripts/review_conflict_resolve.sh`
  (EXIT-trap `gh workflow run internal-orchestrate-poll.yml`), which Phase S3
  generalizes rather than replaces.
- The existing heartbeat/watchdog infrastructure (`scripts/codex_heartbeat.sh`
  + per-script idle watchdogs in `implement.yml` and
  `scripts/review_run_reviewers.sh`) is a partial predecessor to Phase S2's
  per-pid event-idle stall guard; S2 extends coverage rather than starting from
  scratch.

## Goals

Cross-cutting (each falsifiable from the resulting repo + self-test matrix):

1. **Cut wasted LLM tokens** — reuse Codex threads on retries (S4) instead of
   re-rendering full mode prompts; kill zombie sessions on event-idle (S2)
   instead of phase-age.
2. **Cut wasted wall-clock** — force-tick the orchestrator on phase end (S3)
   instead of waiting up to 5 min for the next cron.
3. **Catch silent prompt-contract drift** at render time via a strict renderer
   that fails on unknown/missing/forbidden variables (S1).
4. **Bound blast radius** — per-state concurrency caps (S5) so a regression
   cannot fan out dozens of parallel phase runs.
5. **Make in-flight state legible** — emit run-attempt sub-states into the
   `ai-memory` runs ledger (W4) and publish one snapshot artifact per tick (U3).
6. **Reduce setup churn** — reuse per-issue workspaces across attempts (W1)
   with explicit `after_create` / `before_run` / `after_run` / `before_remove`
   hooks (W2) and filesystem-safety invariants (W3).
7. **Unify the policy surface** — one optional per-consumer overlay file (U1)
   and a runtime blocker check (U2) that hardens dispatch against DAG drift.

## Non-goals

- Replacing GitHub Actions with a long-running daemon. We remain a cron-driven
  workflow set; every adaptation is shaped to fit that model.
- Removing the multi-reviewer panel, the validation harness layer, or the
  `ai-memory` retrieval system.
- Migrating off GitHub Issues; tracker abstraction is out of scope.
- Symphony's open-ended `max_turns` continuation policy. Existing per-phase
  caps (`MAX_AUTOFIX_ITERATIONS`, `MAX_VALIDATE_CYCLES`,
  `MAX_STALL_RECOVERIES_PER_ISSUE`, `MAX_REVIEW_BLOCKED_RETRIES`) are kept as-is.
- Hot-reload of policy files; Actions checks out the latest default branch each
  run.
- An HTTP server / live dashboard. The closest analog is U3's JSON snapshot
  artifact (optionally on an orphan branch), not a live endpoint.
- Tracker-writes-via-agent-tools refactor; the label/comment write boundary is
  mature and out of scope.
- Changing which models are called or their reasoning levels; touching reviewer
  panel composition; reshaping the wave/dependency-DAG decomposition; replacing
  the `workflow_dispatch` tick transport.
- Cross-run Codex thread reuse (S4 reuses a thread only within a single workflow
  run).

## Constraints

This plan is implemented by the unattended orchestrator. The following bind the
design (cited from `CLAUDE.md`; the parallel `unattended_system_instructions.md`
numbering applies to the implementing pipeline):

- **§6 — Naming immutability.** No existing workflow input, env var, label, log
  key, or script entrypoint is renamed or removed. `render_prompt.sh` (S1) is
  **kept as a thin backward-compatible shim** that forwards to the new
  `render_prompt.py`; it is not renamed away. All new env vars are additive; old
  ones are retained for at least one full release after a replacement becomes
  the default.
- **§10 — MongoDB.** **Not applicable.** This plan touches no MongoDB
  collections and adds no `/db/contracts/*.yml`. The only schemas it changes are
  JSON files under `ai-memory/schemas/` (additive open-set ledger event kinds)
  plus two new artifact schemas — these are not §10 contracts.
- **§14 — Consumer-repo registry.** S3, S5, W1, W2, U1, and U3 edit files under
  `workflow-templates/`, which propagate to every repo in
  `.github/ai/consumer_repos.json` via `update_workflows.yml`. Each such phase
  lands the template change in the same PR as its `.github/workflows/*` change;
  propagation follows the normal `@stable` sync. New consumer-authored knob
  files (`.github/ai/concurrency_caps.yml`, `.github/ai/workspace_hooks/`,
  `.github/ai/WORKFLOW.md`) are absent-by-default and opt-in.
- **§15 — GitHub API hygiene.** S5's running-runs count and U2's blocker-state
  check MUST reuse a single cycle-local prefetch per tick rather than fanning
  out per-issue API calls. S5 issues one paginated
  `gh api .../actions/runs?status=in_progress` per tick into a cached
  per-state map; U2 reads from the orchestrator's existing cycle-local
  issue/PR-state cache (the `_fetch_*_graphql` prefetch path) — no new
  per-issue round-trips. U3 assembles its snapshot from data the poller already
  fetched plus the ledger; it adds no per-issue calls.
- **§18 — Automation bias & future-removal registry.** Every new script is
  wired into an existing automated workflow (no manual-invocation scripts). The
  one new standalone workflow (`workspace-cache-maintenance.yml`, W1) is a
  nightly cron, not an operator action. Two artifacts require
  `docs/scripts-pending-removal.md` entries in the PR that introduces them
  (§18.F): the `render_prompt.sh` shim (single-use/transitional; removal
  trigger = all call sites pass `--mode` explicitly) and
  `workspace-cache-maintenance.yml` (long-running; "permanent — review
  annually").

## Approach

Three independently shippable projects, mapped 1:1 from the source doc:

- **Project S — Per-Run Efficiency** (S1–S5): strict rendering, event-based
  stall, force-tick, thread reuse, per-state caps. Lowest risk, fastest
  payback, no architectural change.
- **Project W — Workspace & Lifecycle** (W1–W4): per-issue workspace cache,
  lifecycle hooks, filesystem-safety invariants, run-attempt sub-state ledger
  events.
- **Project U — Policy Surface Unification** (U1–U3): per-consumer `WORKFLOW.md`
  overlay, runtime blocker check, per-tick state snapshot.

Every chunk is **flag-gated and fail-open**: it ships dormant (default-off, or
additive-and-safe for the few default-on chunks) and is enabled only after its
self-test matrix passes. This is the mechanism that makes each chunk
independently mergeable (see [Phases & Merge Strategy](#phases--merge-strategy)).
The cron `*/5 * * * *` schedule and all existing per-phase caps remain as outer
safety nets throughout.

## Phases & Merge Strategy

Each of the twelve chunks is **one phase = one PR**. The orchestrator ships them
as independent PRs; every merge lands directly in production.

**Independent-mergeability model.** The source doc's dependency DAG is an
**enablement (flag-flip) order**, not a **merge order**. Each phase merges with
its feature flag default-off (or, for additive-safe chunks, default-on but inert
when its inputs are absent), so its code is dormant at merge time and the system
stays working regardless of merge order. A dependency means "chunk X must be
*enabled* before chunk Y is *enabled*", never "X's PR must merge before Y's PR".
To hold that invariant, the conversion adds these **graceful-degradation
requirements** (each verified by a flag-off / dependency-absent self-test):

- **W2** treats an unset `CREATED_NOW` (W1 not yet enabled) as `true` — i.e.
  runs `after_create` every invocation, matching W1-absent behavior.
- **W3** enforces workspace-path invariants only when `WORKSPACE_REUSE_ENABLED=true`
  (W1's layout active); it no-ops on the legacy `/tmp/codex-*-${RUN_ID}` paths.
  This is a deliberate change from the source's "always on" wording, made to
  keep W3 mergeable before W1 (recorded in [Risks](#risks--mitigations)).
- **U2** fails open (`eligible=true`) when a sub-issue has no `dependency_edges`
  block, so default-on blocker checking never blocks legacy dispatch.
- **U3** omits the `running[].substate` field when W4 is not enabled; the
  snapshot still ships and validates.
- **S4** continuation prompts/contracts and **U1** overlay are inert until their
  flags are on **and** S1 (strict renderer) / S5 (caps key) are present;
  flag-off they are unreferenced files.

For each phase: **scope**, **files**, **done condition** (proves independent
shippability), **rollback**.

| # | Phase | Default flag state | Done condition (self-test) | Rollback |
|---|---|---|---|---|
| 1 | **S1** strict renderer | `STRICT_PROMPT_RENDER=false` (warn-only) | every mode has a `prompts/contracts/<mode>.yml`; flag-on self-test = zero violations; a deliberately-broken contract fails with structured error | set flag `false`; shim restores today's behavior; contracts are deletable |
| 2 | **S2** event-idle stall guard | `CODEX_STALL_GUARD_ENABLED=false` (observe-only) | stub paused > timeout is killed within 60 s and emits `codex_stall_killed`; phase-age net does not trip first; flag-off = full phase-age window | set flag `false` → observe-only (emits `codex_stall_observed` only) |
| 3 | **S3** force-tick on phase end | `FORCE_TICK_ENABLED=true` | implement.yml PR push → next tick within 30 s (not 5 min); cooldown caps to one tick / 30 s / tracking issue | set flag `false` → cron-only |
| 4 | **S4** continuation-turn reuse | `CODEX_THREAD_REUSE_ENABLED=false` | iteration-2 Codex call uses `thread_id` + continuation prompt < 25% of iteration-1 size; output functionally equivalent to flag-off | set flag `false` → full-prompt path |
| 5 | **S5** per-state concurrency caps | gate = presence of `.github/ai/concurrency_caps.yml` | with `ai:review-blocked` capped at 2, three simultaneous labels → exactly 2 running runs + one `phase_capped` event | remove/empty caps file → today's behavior |
| 6 | **W1** per-issue workspace cache | `WORKSPACE_REUSE_ENABLED=false` | 2nd run for same issue restores workspace, sets `CREATED_NOW=false`, skips one-time setup; retry wall-clock −90 s | set flag `false` → per-run-id path, `CREATED_NOW=true` always |
| 7 | **W2** lifecycle hooks | gate = hook-file presence | non-empty `after_create.sh` runs once per workspace lifetime; failing `before_run.sh` aborts with structured error | remove hook files → no-op |
| 8 | **W3** filesystem-safety check | enforced iff `WORKSPACE_REUSE_ENABLED=true` | `WORKSPACE_PATH=/tmp/escape` aborts before Codex launch + emits `workspace_safety_violation`; real flows pass | gated by W1's flag; no-op when W1 off |
| 9 | **W4** run-substate ledger events | `LEDGER_SUBSTATES_ENABLED=true` | a full implement run emits ordered substates `PreparingWorkspace`→`Succeeded`/terminal; reconstructable from ledger | set flag `false`; events are additive |
| 10 | **U1** `WORKFLOW.md` overlay | gate = presence of `.github/ai/WORKFLOW.md` | overlay setting `max_autofix_iterations: 1` caps iterations; unknown key fails validation with structured error | remove file → template defaults |
| 11 | **U2** runtime blocker check | `RUNTIME_BLOCKER_CHECK_ENABLED=true` | sub-issue with one open blocker is deferred + emits `dispatch_deferred_blocker`; after blocker `ai:merged`, next tick dispatches | set flag `false` → DAG-only dispatch |
| 12 | **U3** per-tick state snapshot | `STATE_SNAPSHOT_ARTIFACT_ENABLED=true`; `STATE_SNAPSHOT_BRANCH_ENABLED=false` | every tick uploads a `state.json` passing `state_snapshot.v1.json`; reflects all `ai:orchestrator-tracking` issues | set artifact flag `false`; branch publish is opt-in |

**Enablement waves** (flag-flip order after each chunk's PR has merged and baked):

- **Wave 1 (parallel):** S1, S2, S3, S5, W1, W4, U2 — all independent; together
  the minimum viable Symphony-aligned baseline.
- **Wave 2 (parallel, after Wave 1 enabled):** S4 (needs S1's continuation
  contracts), W2 (needs `CREATED_NOW` from W1), W3 (needs W1's layout), U3
  (consumes W4 substates).
- **Wave 3:** U1 (overlay needs S1's renderer + S5's caps key + W2's hooks dir).

## Implementation Steps

Grouped by phase. Each step names the files touched and the change in one
sentence. No step straddles a phase boundary. Steps within a phase land as
individual commits in that phase's PR.

### Phase 1 — S1: strict prompt rendering

1. Add `scripts/render_prompt.py` — a strict renderer (Mustache via `chevron`,
   or `jinja2` with `StrictUndefined, extensions=[]`) that loads a per-mode
   contract, renders, and fails non-zero on `missing_required`,
   `unknown_in_template`, or `forbidden_present`.
2. Add `prompts/contracts/<mode>.yml` for every mode in `prompts/mode-*.txt`
   (and the review/judge/conflict prompts), declaring required vars, optional
   vars with defaults, and forbidden vars.
3. Have the renderer write a render-receipt JSON beside the rendered prompt:
   contract version, variable values used (secret keys redacted), prompt body
   SHA.
4. Rewrite `scripts/render_prompt.sh` as a thin shim that calls
   `render_prompt.py --legacy-mode-name <mode>` (§6: shim preserved one
   release).
5. Gate enforcement on `STRICT_PROMPT_RENDER` (default `false` = warn-only).
6. Add `tests/render_prompt/*` golden tests per mode (flag-on and flag-off).
7. Add a `docs/scripts-pending-removal.md` entry for the `render_prompt.sh`
   shim (§18.F): type single-use/transitional; removal trigger = all call sites
   pass `--mode` explicitly; preflight = grep shows no `render_prompt.sh`
   invocations without `--mode`.

### Phase 2 — S2: Codex-event-based stall detection

1. Have every Codex-invoking site write a **per-pid** heartbeat
   `${RUNNER_TEMP}/codex-heartbeats/codex-${pid}.json`
   (`{run_id, issue, mode, last_event_at, last_event_kind, pid}`), updated per
   protocol event (per-pid filenames avoid clobber across the five parallel
   reviewers).
2. Add `scripts/codex_stall_guard.sh` — a background sidecar that scans the
   heartbeat dir, trips on `now - last_event_at > CODEX_STALL_TIMEOUT_SECONDS`
   (default `600`), SIGTERMs the matching pid then SIGKILLs after
   `CODEX_STALL_KILL_GRACE_SECONDS` (default `30`).
3. Add a `codex_run_with_stall_guard()` wrapper to each invoking script that
   runs Codex, captures and `wait`s the exit code, emits `codex_stall_killed`
   on a guard-side `137`, and propagates the non-zero code (so a killed
   background Codex never lets the script continue silently). Wire into
   `scripts/review_run_reviewers.sh`, `scripts/review_apply_fixes.sh`,
   `scripts/review_conflict_resolve.sh`, `scripts/self_heal_validation.sh`,
   `scripts/review_rb_judge.sh`, `scripts/run_validation_repo_checks.sh`, and
   the `implement.yml` Codex step.
4. Raise the `orchestrate_poll_process.sh` phase-age net to `90` minutes (outer
   safety net) once event-based killing is primary.
5. Gate on `CODEX_STALL_GUARD_ENABLED` (default `false` = observe-only, emits
   `codex_stall_observed`). Add new env keys to
   `.github/workflows/orchestrate_poll.yml`.
6. Add a self-test that pauses a Codex stub for `timeout + 60 s`.

### Phase 3 — S3: force-tick on phase end

1. Add `scripts/orchestrate_force_tick.sh` invoking
   `gh workflow run internal-orchestrate-poll.yml` with
   `{reason, source_workflow, issue, run_id}` (same dispatch surface the
   resolver already uses).
2. Make it idempotent via a cooldown timestamp on the `ai-memory` branch at
   `runs/force_tick/<tracking_issue>.json` (using `scripts/memory_helpers.sh`);
   no-op within `FORCE_TICK_COOLDOWN_SECONDS` (default `30`) — keeps state on
   the existing persistent surface, no new API round-trips (§15).
3. Call it from EXIT traps in `.github/workflows/implement.yml` (after PR push),
   `.github/workflows/review_autofix.yml` (after merge or `ai:review-blocked`),
   `.github/workflows/validate.yml` (after final pass/fail label), and upgrade
   `scripts/review_conflict_resolve.sh` to use the helper instead of the inline
   dispatch.
4. Gate on `FORCE_TICK_ENABLED` (default `true`). Mirror the workflow edits into
   `workflow-templates/` (§14).

### Phase 4 — S4: continuation-turn reuse

1. **Capability probe (first step).** Add a one-shot probe of pinned
   `@openai/codex@v0.114.0` thread/resume support (e.g. `codex exec resume
   --help` / a smoke turn). Document **both** branches in the phase: (a)
   thin-flag-flip if `codex exec` exposes thread reuse, (b) a JSON-RPC
   app-server wrapper if reuse is only available via the protocol. The
   implementer selects the branch from the probe result. (Resolves the source's
   open question deterministically at build time — see [Risks](#risks--mitigations).)
2. First attempt invokes Codex with no `thread_id` and the full rendered prompt;
   the wrapper records the assigned `thread_id` to
   `${RUNNER_TEMP}/codex-thread.<phase>.json`.
3. Subsequent **same-run** attempts pass that `thread_id` and render only the
   delta from a new `prompts/mode-<phase>-continuation.txt`
   (`mode-implement-repair-continuation.txt`,
   `mode-implement-diagnose-continuation.txt`,
   `mode-validate-self-heal-continuation.txt`,
   `mode-review-apply-fixes-continuation.txt`,
   `mode-review-conflict-resolver-continuation.txt`), each with a
   `prompts/contracts/` contract (S1).
4. Cross-run reuse stays out of scope (each workflow run starts a fresh thread).
5. Wire into `scripts/review_apply_fixes.sh`,
   `scripts/review_conflict_resolve.sh`, `scripts/self_heal_validation.sh`, and
   the implement.yml retry block. Gate on `CODEX_THREAD_REUSE_ENABLED` (default
   `false`).

### Phase 5 — S5: per-state concurrency caps

1. Add `.github/ai/concurrency_caps.yml` (`max_concurrent_by_state` per `ai:*`
   state, `global_max_concurrent`; per-state `-1` disables that cap).
2. At the **start** of each tick, prefetch running runs via one paginated
   `gh api .../actions/runs?status=in_progress` into a per-state count map held
   for the tick (optionally written to
   `${RUNTIME_DIR}/running_runs_by_state.json`) so per-issue decisions are O(1)
   lookups (§15).
3. Before dispatching a phase action, consult the map; if state ≥ cap, defer to
   next tick and emit `phase_capped`. Caps gate dispatch only; in-flight runs
   are never killed.
4. Edit `scripts/orchestrate_poll_process.sh` and `scripts/orchestrate_lib.py`;
   mirror into `workflow-templates/`. Gate = caps-file presence (missing/empty =
   today's behavior).

### Phase 6 — W1: per-issue workspace cache

1. Add `scripts/workspace_init.sh` computing
   `WORKSPACE_KEY = sanitize(issue_identifier)` (`[A-Za-z0-9._-]`, else `_`) and
   `WORKSPACE_PATH = ${RUNNER_TEMP}/workspaces/${WORKSPACE_KEY}`, with a
   path-prefix guard.
2. Use `actions/cache@v4` with
   `key: workspace-v1-${WORKSPACE_KEY}-${WORKSPACE_FINGERPRINT}-${{ github.run_id }}`
   (`WORKSPACE_FINGERPRINT = hashFiles('package-lock.json', '.ai/validate.yml',
   'package.json')`) and ordered `restore-keys` (exact-fingerprint prefix, then
   issue-only prefix) — an append-and-evolve cache where `run_id` guarantees the
   post-job save always writes a fresh entry.
3. Export `CREATED_NOW=false` only on a first-tier (exact-fingerprint) restore;
   `true` on a complete miss or looser-prefix match.
4. Add nightly `.github/workflows/workspace-cache-maintenance.yml` calling
   `gh actions caches list` and deleting all but the newest 3 entries per
   `workspace-v1-<key>-<fingerprint>-` prefix and all entries for closed
   tracking-issue keys (bounds spend to ≈`3 × workspace_size`).
5. Implement `WORKSPACE_BACKEND={cache,branch}` (default `cache`) with the
   sibling orphan-branch fallback (`ai-workspaces/<WORKSPACE_KEY>`) and a
   `cache_evicted_unexpectedly` ledger event for the canary watch.
6. Gate on `WORKSPACE_REUSE_ENABLED` (default `false` → per-run-id path,
   `CREATED_NOW=true`). Edit `implement.yml`, `validate.yml`, `review_autofix.yml`
   (resolver step); mirror into `workflow-templates/`.
7. Add a `docs/scripts-pending-removal.md` entry for
   `workspace-cache-maintenance.yml` (§18.F): type long-running; trigger
   "permanent — review annually".

### Phase 7 — W2: workspace lifecycle hooks

1. Add `scripts/run_workspace_hook.sh` reading
   `.github/ai/workspace_hooks/<phase>/{after_create,before_run,after_run,before_remove}.sh`,
   running with `bash -lc`, cwd `WORKSPACE_PATH`, timeout
   `WORKSPACE_HOOK_TIMEOUT_SECONDS` (default `600`).
2. Failure semantics: `after_create` / `before_run` fatal; `after_run` /
   `before_remove` logged-and-ignored. Capture stdout/stderr to
   `${RUNNER_TEMP}/workspace-hooks/<phase>-<hook>.log` (last 10 KB on failure).
3. Gate `after_create` on `CREATED_NOW=true`; treat **unset** `CREATED_NOW` as
   `true` (independent-mergeability before W1).
4. Add a placeholder `.github/ai/workspace_hooks/` tree; wire invocations into
   `implement.yml` and `validate.yml`; mirror into `workflow-templates/`. Gate =
   hook-file presence.

### Phase 8 — W3: filesystem-safety invariants

1. Add `scripts/workspace_safety_check.sh` verifying (a) `WORKSPACE_PATH`
   resolves under `${RUNNER_TEMP}/workspaces/` (realpath), (b) `pwd -P` ==
   resolved `WORKSPACE_PATH`, (c) `WORKSPACE_KEY` matches `^[A-Za-z0-9._-]+$`;
   abort with exit 78 + `workspace_safety_violation` on failure.
2. Invoke immediately before each Codex launch in implement, validate,
   review-autofix, and conflict-resolver paths.
3. Enforce only when `WORKSPACE_REUSE_ENABLED=true` (no-op on legacy paths) so
   W3 is mergeable before W1.

### Phase 9 — W4: run-attempt sub-state ledger events

1. Add `scripts/ledger_emit_substate.sh` writing one
   `{ts, kind:"run_substate", substate, phase, issue, mode}` line to
   `ai-memory:runs/${RUN_ID}/ledger/events.jsonl`; idempotent on duplicate
   substate within one invocation.
2. Call it at phase boundaries (`PreparingWorkspace`, `BuildingPrompt`,
   `LaunchingAgentProcess`, `InitializingSession`, `StreamingTurn`, `Finishing`,
   `Succeeded`/`Failed`/`TimedOut`/`Stalled`) from the same Codex-invoking
   scripts as S2.
3. Confirm the open-set rule in `ai-memory/schemas/run_ledger_entry.v1.json`
   admits new `kind` values additively; add an `ai_memory_lib.py` helper only if
   needed. Gate on `LEDGER_SUBSTATES_ENABLED` (default `true`).

### Phase 10 — U1: `WORKFLOW.md` overlay

1. Add `scripts/load_workflow_overlay.py` parsing
   **`.github/ai/WORKFLOW.md`** (resolved location — see
   [Risks](#risks--mitigations); not repo root) with optional YAML front matter
   + Markdown body, validating against
   `ai-memory/schemas/workflow_overlay.v1.json`, exporting resolved values via
   `$GITHUB_ENV`; unknown keys fail validation.
2. Support `limits.*` (incl. `max_concurrent_by_state` surfacing S5),
   `prompt_overrides[].{mode,append_path,replace_path}` applied by the S1
   renderer (subject to the per-mode contract), and `workspace.hooks_dir`
   (surfacing W2).
3. Call early from `orchestrate_poll.yml`, `implement.yml`, `plan.yml`,
   `clarify.yml`, `validate.yml`, `review_autofix.yml`; mirror into
   `workflow-templates/`; document in `README.md`. Gate = file presence
   (`WORKFLOW_OVERLAY_ENABLED` implicit). Absent file = today's behavior.

### Phase 11 — U2: blocker-aware runtime dispatch

1. Add `scripts/blocker_check.py` reading a sub-issue's `dependency_edges` from
   the tracking-issue body, returning `eligible=true` only if every blocker is
   terminal (`ai:merged`, `ai:closed`, or PR merged); **fail open** when no
   `dependency_edges` block exists.
2. Consult it before dispatch in `scripts/orchestrate_poll_process.sh` (reusing
   the cycle-local prefetched issue/PR-state cache — §15); on `eligible=false`,
   emit `dispatch_deferred_blocker` and skip for the tick.
3. Edit `scripts/orchestrate_lib.py` as needed. Gate on
   `RUNTIME_BLOCKER_CHECK_ENABLED` (default `true`).

### Phase 12 — U3: per-tick state snapshot

1. Add `scripts/build_state_snapshot.py` assembling `state.json`
   (`tick_at`, `tracking_issues[]`, `running[]`, `deferred[]`, `totals`) from
   the poller's existing label aggregation + the W4 substate ledger.
2. **Token totals accumulator (resolved scope addition).** Because the ledger
   has no token fields today and `cost_audit.py` only parses tokens post-hoc
   from Actions logs, add a lightweight per-run token accumulator that records
   `input`/`output` token counts (parsed from the Codex "tokens used" line /
   OpenRouter usage line at run end) into the ledger so `running[].tokens` and
   `totals` are populatable. Omit `running[].substate` when W4 is disabled.
3. Validate against `ai-memory/schemas/state_snapshot.v1.json`; upload as
   workflow artifact `state-snapshot` every tick; optionally force-push to a
   `state-snapshot` orphan branch (last `STATE_SNAPSHOT_HISTORY_DEPTH` ticks,
   default `100`) when `STATE_SNAPSHOT_BRANCH_ENABLED=true`.
4. Edit `scripts/orchestrate_poll_process.sh` and
   `.github/workflows/orchestrate_poll.yml`; mirror into `workflow-templates/`.
   Gate on `STATE_SNAPSHOT_ARTIFACT_ENABLED` (default `true`).

## Files & Modules

New scripts: `scripts/render_prompt.py` `[new]`,
`scripts/codex_stall_guard.sh` `[new]`,
`scripts/orchestrate_force_tick.sh` `[new]`,
`scripts/workspace_init.sh` `[new]`,
`scripts/run_workspace_hook.sh` `[new]`,
`scripts/workspace_safety_check.sh` `[new]`,
`scripts/ledger_emit_substate.sh` `[new]`,
`scripts/load_workflow_overlay.py` `[new]`,
`scripts/blocker_check.py` `[new]`,
`scripts/build_state_snapshot.py` `[new]`.

New config / schema / prompt assets: `prompts/contracts/*.yml` (one per mode)
`[new]`, `prompts/mode-*-continuation.txt` (×5) `[new]`,
`.github/ai/concurrency_caps.yml` `[new]`,
`.github/ai/workspace_hooks/<phase>/*.sh` placeholders `[new]`,
`.github/ai/WORKFLOW.md` (consumer-authored; documented, not committed here)
`[new, optional]`, `ai-memory/schemas/workflow_overlay.v1.json` `[new]`,
`ai-memory/schemas/state_snapshot.v1.json` `[new]`.

New workflows: `.github/workflows/workspace-cache-maintenance.yml` `[new]`.

Edited scripts: `scripts/render_prompt.sh` (→ shim), `scripts/orchestrate_lib.py`,
`scripts/orchestrate_poll_process.sh`, `scripts/review_run_reviewers.sh`,
`scripts/review_apply_fixes.sh`, `scripts/review_conflict_resolve.sh`,
`scripts/self_heal_validation.sh`, `scripts/review_rb_judge.sh`,
`scripts/run_validation_repo_checks.sh`, `scripts/ai_memory_lib.py` (if needed),
`ai-memory/schemas/run_ledger_entry.v1.json` (additive `kind` values).

Edited workflows (+ mirrored `workflow-templates/` wrappers): `implement.yml`,
`review_autofix.yml`, `validate.yml`, `orchestrate_poll.yml`, `plan.yml`,
`clarify.yml`.

Edited docs: `README.md` (new flags + ledger event reference + overlay),
`agents.md` (sub-state vocabulary + overlay schema rows),
`docs/scripts-pending-removal.md` (shim + maintenance workflow entries).

Deleted (this PR's housekeeping): `docs/symphony-inspired-improvements.md`
`[del]` — plus link repoints across `docs/plans/open-agents-learnings-plan.md`,
`docs/plans/gsd-inspired-improvements-plan.md`,
`docs/plans/gstack-learnings-plan.md`,
`docs/plans/claude-code-tooling-learnings-plan.md`,
`docs/ai-tools-future-improvements.md`,
`docs/completed/apply-ai-tools-learnings-plan.md`.

## Data Model / Schema Changes

**No MongoDB collections or `/db/contracts/*.yml` touched (§10 N/A).**

JSON schema changes, all under `ai-memory/schemas/`:

- `run_ledger_entry.v1.json` — additive new `kind` values
  (`codex_stall_observed`, `codex_stall_killed`, `phase_capped`,
  `dispatch_deferred_blocker`, `run_substate`, `workspace_safety_violation`,
  `cache_evicted_unexpectedly`) plus optional per-run token fields for U3. No
  version bump — relies on the schema's documented open-set rule (verify before
  relying on it; if the schema is closed-set, bump to `v2` additively and keep
  `v1` readable).
- `workflow_overlay.v1.json` `[new]` — typed, strict (unknown keys rejected),
  versioned for additive-only evolution.
- `state_snapshot.v1.json` `[new]` — schema for the U3 artifact.

## Tests

- **Per-chunk golden self-tests under `tests/`**, exercising flag-on and
  flag-off paths for every chunk (S1 renderer contract violations; S2
  stall-stub kill timing; S3 force-tick latency + cooldown; S4 continuation
  prompt-size + output equivalence; S5 cap enforcement + `phase_capped`; W1
  restore + `CREATED_NOW`; W2 once-per-lifetime + fatal `before_run`; W3 escape
  abort; W4 ordered substates; U1 overlay cap + unknown-key rejection; U2
  deferral + recovery; U3 artifact schema validity).
- **Dependency-absent self-tests** proving independent-mergeability: W2 with
  unset `CREATED_NOW`; W3 with `WORKSPACE_REUSE_ENABLED=false`; U2 with no
  `dependency_edges`; U3 with W4 disabled.
- **Interaction matrix.** `nightly-validation-selftest.yml` runs all chunks
  flag-on against fixture issues to catch interaction bugs before defaults are
  flipped.
- Schema tests for the new/edited `ai-memory/schemas/*.json`.

## Risks & Mitigations

- **S4 — Codex `thread_id` CLI support unknown** (source open question, resolved
  via probe-first per clarification). The phase's first step probes
  `@openai/codex@v0.114.0` and selects the thin-flag-flip vs JSON-RPC-wrapper
  branch deterministically. `ACCEPTED — pending build-time probe`; per-phase
  flag gating + the live full-prompt path bound the blast radius.
- **W1 — `actions/cache` 10 GB per-repo budget under steady-state load**
  (source open question). Mitigated by versioned keys + `restore-keys` +
  nightly maintenance, with the `WORKSPACE_BACKEND=branch` fallback wired and a
  `cache_evicted_unexpectedly` ledger event. `ACCEPTED — pending one-week
  flag-on canary` cache-usage telemetry before defaulting `WORKSPACE_REUSE_ENABLED`
  on; flip to the branch backend if eviction persists.
- **U1 location decision.** Resolved to `.github/ai/WORKFLOW.md` (repo
  convention — other AI knobs already live under `.github/ai/`), deliberately
  diverging from the source's "repo root" (chosen there for visibility). The
  loader checks only the `.github/ai/` path.
- **U3 token-source decision.** Resolved: add a new additive token accumulator
  rather than "consume existing memory-layer totals" — the ledger has no token
  fields today and `cost_audit.py` is post-hoc log parsing only.
- **W3 "always on" softened to W1-gated.** Deliberate change from the source so
  W3 is mergeable before W1; once `WORKSPACE_REUSE_ENABLED=true`, W3 enforces as
  the source intended.
- **S2 false-positive kills** of legitimately-slow sessions if
  `CODEX_STALL_TIMEOUT_SECONDS` is too low. Mitigated by observe-only default
  and sizing the timeout from real-run p99 inter-event delay before flipping.
- **S1 ↔ gstack Phase O overlap.** Both define a prompt-templating engine.
  Whichever lands first ships the engine; the other reuses it — do **not**
  implement two renderers. Coordinate at S1 implement time by checking
  `docs/plans/gstack-learnings-plan.md` Phase O status.
- **§6 naming.** `render_prompt.sh` is preserved as a shim (no rename). All new
  identifiers are additive. No existing env var / label / input is renamed.
- **Schema open-set assumption.** If `run_ledger_entry.v1.json` is closed-set,
  the additive `kind` values require a careful `v2` bump that keeps `v1`
  readers working — verify before W4/S2 implementation.

## Rollout

- **Merge order:** free — every phase merges flag-off/inert (see
  [Phases](#phases--merge-strategy)). The default-on chunks (S3, W4, U2, U3) are
  additive and fail-open with their inputs absent.
- **Enablement order:** Wave 1 (S1, S2, S3, S5, W1, W4, U2) → Wave 2 (S4, W2,
  W3, U3) → Wave 3 (U1). Flip each flag only after its PR has merged, baked, and
  passed the interaction matrix.
- **Canary watches before defaulting on:** S2 (`CODEX_STALL_TIMEOUT_SECONDS`
  from p99 data), W1 (one week of cache-usage telemetry vs the 10 GB cap).
- **Consumer-repo propagation (§14):** template wrappers under
  `workflow-templates/` for S3, S5, W1, W2, U1, U3 propagate to repos in
  `.github/ai/consumer_repos.json` via `update_workflows.yml` on the next
  `@stable` sync; the consumer-authored knob files
  (`concurrency_caps.yml`, `workspace_hooks/`, `WORKFLOW.md`) are opt-in and
  absent by default, so propagation is behavior-neutral until a consumer adds
  them.
- **Rollback:** per-chunk, via the flag in the table above; no chunk requires
  another to be reverted first.

## This PR's housekeeping

Per the task ("convert … and then remove the original doc") and the explicit
clarification answer (hard-delete + repoint references), this PR — in addition
to adding this plan file — **deletes** `docs/symphony-inspired-improvements.md`
and repoints its ~10 inbound references (across `open-agents-learnings-plan.md`,
`gsd-inspired-improvements-plan.md`, `gstack-learnings-plan.md`,
`claude-code-tooling-learnings-plan.md`, `ai-tools-future-improvements.md`, and
`completed/apply-ai-tools-learnings-plan.md`) to
`docs/plans/symphony-inspired-improvements-plan.md`. This is documentation-only
housekeeping; no source, config, workflow, script, schema, or contract behavior
changes in this PR. The Symphony implementation work described above ships later
as the twelve phased PRs.

## References

- OpenAI Symphony orchestration spec (`openai/symphony` SPEC.md, March 2026) —
  source of the borrowed mechanisms.
- Predecessor in-repo: `scripts/review_conflict_resolve.sh` (force-tick EXIT
  trap), `scripts/codex_heartbeat.sh` + `implement.yml` / `review_run_reviewers.sh`
  watchdogs (heartbeat/stall precedent).
- Sibling plan: `docs/plans/gstack-learnings-plan.md` (Phase O overlaps S1).
- `.github/actions/install-codex/action.yml` (`@openai/codex@v0.114.0` pin).
- `ai-memory/schemas/run_ledger_entry.v1.json` (ledger schema extended by S2 /
  S5 / W3 / W4 / U2 / U3).
- CLAUDE.md §6 (naming), §10 (MongoDB), §14 (consumer registry), §15 (API
  hygiene), §18/§18.F (automation bias / future-removal registry).
