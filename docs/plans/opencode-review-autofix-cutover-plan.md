# Opencode Cutover of the review_autofix Workflow

## Summary

Replace the Codex CLI with opencode as the agent runtime for every LLM call
site in `review_autofix.yml` — reviewer panel, consensus summariser, editor,
RB judge, consolidator, and merge-conflict resolver — as an instant cutover
on merge (no feature flag), with `@stable` propagation to consumer repos
gated on a 3-consecutive-run production parity criterion. Codex remains the
runtime for every other workflow (clarify, plan, implement, orchestrate,
validate, workflow-log-analysis, security-audit, check-failure-triage).

## Context

The repo pins Codex CLI `v0.114.0` (published 2026-03-11, ~5.5 months and
35 minor releases behind upstream 0.149.1). Two upgrade attempts failed:

- PR #1704 bumped to v0.125.0; within hours 3 of 6 reviewer models
  (DeepSeek, Qwen, x-ai) hard-failed with HTTP 422 because Codex ≥0.122
  wraps MCP tools in a `type: "namespace"` envelope on the OpenAI
  Responses API that those vendors' OpenRouter adapters reject.
- PR #1717's strip-MCP workaround caused empty reviewer output; PR #1729
  reverted the pin ~17 hours after the bump. PR #1752 later flipped
  `apply_patch_tool_type` to `"function"` in the model catalog because the
  same vendors also reject Codex's `type: "custom"` apply_patch tool.

Sandboxed testing on 2026-08-26 (Codex 0.114.0 / 0.149.1 and opencode
1.18.23 run against a local wire-capture server) established:

- Codex 0.149.1 rejects the current model catalog at startup (`unknown
  variant 'function', expected 'freeform'` — 11 entries), still emits the
  `namespace` MCP envelope, adds a new always-on `multi_agent_v1`
  namespace tool, and emits apply_patch as `type: "custom"` with no
  `function` escape hatch. The failure class that killed both upgrade
  attempts is structural to Codex's Responses-API wire shape.
- opencode 1.18.23 speaks `/chat/completions` with **every tool emitted as
  plain `type: "function"`** — MCP tools and apply_patch included. This is
  the vendor-neutral wire shape all OpenRouter providers accept; the
  namespace/custom 422 class cannot occur by construction.
- opencode verified: headless `opencode run` with stdin prompts (300 KB
  delivered byte-intact), `-m openrouter/<slug>` per-call model selection,
  `--variant <effort>` reasoning control, `--session`/`--continue` resume,
  `--format json` structured events, MCP via config (stdio server worked
  first try), clean fast-fail on HTTP 4xx (no retry hang).
- opencode caveats found: output carries ANSI escapes; the session-title
  agent sends the **full prompt** to a hardcoded small model unless a
  fixed `--title` is passed and `small_model` is configured; first run
  fetches the models.dev catalog (needs CI cache warm); the reasoning
  effort appeared on the wire as a top-level `reasoningEffort` key whose
  OpenRouter-side handling must be confirmed with a live key.

User-approved decisions (clarification rounds, this plan's authority):

| ID | Decision |
|---|---|
| Q1: C / F1: A | All six Codex call sites in review_autofix switch to opencode |
| Q2: C / F2: A | No flag. Merge = cutover for this repo's `main` runs; `@stable` (consumer propagation) tagged only after the parity criterion passes |
| Q3: A | Parity criterion: 3 consecutive real review_autofix runs with `REVIEWERS_SUCCESSFUL=6/6`, no new failure classes in the ledger, latency + cache telemetry within ~20% of the Codex baseline |
| Q4: A | Live-key validation runs as a `workflow_dispatch` smoke workflow (no operator shell steps, §18) |
| Q5: A | Reviewer permission posture: `edit: deny`, bash allowed; existing pre/post git-state guards remain the enforcement layer |
| Q6: B | No Codex failback on model-call failures; failures alert via Telegram instead |
| F3: A | Writers (editor, judge-fix, resolver, consolidator): full tool allow — parity with today's `danger-full-access` |
| F4: B | Bootstrap failure (opencode uninstallable/binary missing): hard-fail the job with an admin alert; Codex is fully removed from review_autofix's install path (in P3) |
| F5: A / F6: A | Ordered 3-phase DAG — P2 and P3 depend on P1; explicit, user-authorized deviation from strict phase independence |

## Goals

- Zero Codex invocations remain in `review_autofix.yml` and its six support
  scripts after P3 merges; all agent calls run opencode at a pinned version.
- Reviewer panel keeps all 6 roster slugs (minimax-m3, kimi-k3,
  deepseek-v4-pro, mistral-small-2603, qwen3.7-plus, grok-4.6) and the
  editor keeps `MODEL_EDITOR` / `MODEL_EDITOR_FALLBACK` semantics unchanged.
- Reasoning step-down schedules, two-pass review, stall-guard supervision,
  retry classification, ledger/consolidator contracts, write guards, and
  resolver allowlist/fingerprint guards all keep their current behavior.
- A dispatchable live-key smoke workflow proves per-vendor acceptance and
  reasoning-effort delivery before P2/P3 merge.
- Every opencode-path failure (model-call or bootstrap) emits a Telegram
  ERROR alert with a stable log prefix.
- `@stable` is tagged only after the Q3:A parity criterion passes on `main`.

## Non-goals

- No change to clarify, plan, implement, orchestrate, orchestrate_poll,
  orchestrate_clarify_respond, validate, workflow-log-analysis,
  security-audit, or check-failure-triage — they stay on Codex v0.114.0.
- No Codex version bump (tracked separately; see the 2026-08-26 upgrade
  analysis in this session's findings — the mitigations are known if ever
  needed).
- No model roster changes, no prompt-content redesign beyond the minimal
  apply_patch-wording neutralisation in review-phase prompts (P3).
- No removal of Codex-named identifiers, scripts, or env vars (§6): the
  `install-codex` action, `write_codex_config.sh`, `CODEX_*` env vars, and
  the stall-guard/heartbeat helper names all remain.
- No changes to the thread-reuse feature semantics: `CODEX_THREAD_REUSE_ENABLED`
  (default `false`) keeps its name and its documented fail-open-to-fresh-prompt
  contract; mapping it onto opencode `--session` is future work.

## Constraints

- **§6 naming immutability** — no existing identifier is renamed or
  removed. All new identifiers (`OPENCODE_VERSION`, `write_opencode_config.sh`,
  `opencode_helpers.sh`, `install-opencode`, `opencode_agent_failure` log
  prefix) are checked for uniqueness; none collide with existing names.
  The Codex-named wrappers (`codex_stall_guard.sh`, `codex_heartbeat.sh`)
  are reused as-is — they supervise an arbitrary `-- <cmd>` and their names
  are contractual.
- **§14 consumer repos** — `review_autofix.yml` is a reusable workflow
  consumed by the 13 repos in `.github/ai/consumer_repos.json` via
  `@stable`. Propagation timing is governed by F2:A (see Rollout).
- **§15 GitHub API hygiene** — this plan adds no new `gh api` calls. The
  smoke workflow calls OpenRouter, not GitHub.
- **§18 automation bias** — the live-key smoke is a `workflow_dispatch`
  workflow, not a manual script; alerting reuses `scripts/tg_helpers.sh`.
  §18.F registry entries are added for the new workflow and helpers.
- **§20 changelog** — each phase ships its own `changelog.d/` fragment.
- **§10 MongoDB** — not applicable; no collections touched.
- **Security** — reviewers run with `edit: deny` (Q5:A); writers run full
  allow on ephemeral GH-hosted runners with the existing write-guard and
  resolver-allowlist boundaries unchanged (F3:A). `OPENROUTER_API_KEY`
  handling is unchanged (env-key reference only, never printed).

## Approach

opencode replaces Codex per call site with a thin, symmetric substitution:

| Codex mechanism | opencode replacement |
|---|---|
| `codex --ask-for-approval never … exec --model <slug> --sandbox <mode>` reading stdin | `opencode run --dir <workdir> -m openrouter/<slug> --variant <effort> --title <fixed>` reading stdin |
| `~/.codex/config.toml` via `write_codex_config.sh` | per-role JSON config via `write_opencode_config.sh` (provider baseURL/env-key, `small_model`, `permission`, `mcp`, `tools`, exact-version metadata), selected via `OPENCODE_CONFIG` |
| `model_reasoning_effort` sed-patching per attempt | `--variant <effort>` per invocation (no config rewriting) |
| `--sandbox read-only` (reviewers, judge) | `permission: { edit: deny }`, bash allowed (Q5:A) |
| `--sandbox danger-full-access` (writers) | full tool allow / `--auto` (F3:A) |
| Per-reviewer `CODEX_HOME` copy + MCP strip | per-slot config JSON; MCP strip helpers become dead code but are retained (§6) with a comment |
| `model_catalog_json` | `provider.openrouter.models.<slug>` limit/options entries in the generated config |
| Serena MCP via `[mcp_servers.serena]` TOML | Serena via the config's `mcp` JSON block (same `setup_serena.sh` bootstrap decides availability) |
| stderr heartbeat for stall guard | `--print-logs --log-level INFO` so stderr stays live for the existing activity-file heartbeat |
| Codex stdout consumed by `File:`/`Problem:` marker greps | same files, ANSI-stripped via `opencode_helpers.sh` (`NO_COLOR=1` plus a strip filter) |

Alternatives considered: bumping Codex to 0.149.1 with mitigations
(rejected by Q1:C/Q2:C — the Responses-API wire-shape class recurs with
each Codex release and the user chose the structural fix); a flag-gated
pilot (rejected by Q2:C); aider/goose/Claude Code (rejected in research —
they cannot drive the multi-vendor OpenRouter roster with MCP + headless
parity the way opencode verifiably can).

## Phases & Merge Strategy

**Explicit deviation from strict phase independence, authorized by F5:A +
F6:A:** P2 and P3 depend on P1's shared plumbing. The orchestrator's
dependency DAG MUST declare `P1 → P2` and `P1 → P3` edges (P2 ∥ P3 once P1
merges). No Codex fallback exists after cutover (F4:B), so a P2/P3 run
that somehow executes without P1's artifacts hard-fails with an admin
alert rather than silently degrading. Each phase is production-safe when
merged in DAG order; each is revertible on its own.

1. **P1 — inert opencode plumbing.** New install action, config writer,
   output/alert helpers, unit tests, the live-key smoke workflow, docs and
   registry entries. Nothing in any production path invokes opencode.
   *Done:* CI green; smoke workflow dispatches successfully with a live
   key and reports per-slug results; zero behavior change in any workflow
   run. *Rollback:* revert the P1 PR (nothing depends on it until P2/P3).
2. **P2 — read-side cutover.** Reviewer panel (both passes, capacity/cache
   probe) and consensus summariser switch to opencode. Editor, judge,
   consolidator, resolver still run Codex; the Codex install step remains.
   *Done:* release gate green; a real review_autofix run shows all 6
   reviewer slots and the summariser on opencode with ledger contracts
   intact. *Rollback:* revert the P2 PR (read side returns to Codex; P1
   and P3 unaffected — P3's write side never referenced P2's edits).
3. **P3 — write-side cutover + Codex removal from review_autofix.**
   Editor/apply-fixes, RB judge (+ judge-fix retries), consolidator, and
   merge-conflict resolver switch to opencode; the Codex install step and
   `write_codex_config.sh` calls are removed from `review_autofix.yml`
   only (the action and script themselves remain for other workflows, §6).
   *Done:* `grep -c 'codex' review_autofix.yml` shows no invocation sites;
   release gate green including conflict-resolver and e2e smoke phases.
   *Rollback:* revert the P3 PR — the revert restores the Codex install
   step and write-side invocations in the same commit, returning the
   write side to Codex without touching P2's read side.

## Implementation Steps

### Phase 1 — inert plumbing

1. `.github/actions/install-opencode/action.yml` `[new]` — composite
   action installing `opencode-ai@${OPENCODE_VERSION}` (input default
   pinned to the exact version validated by the smoke workflow, starting
   at `1.18.23`) via npm with `--no-audit --no-fund`, warming the
   models.dev catalog cache (one `opencode run --help` + a version probe),
   and failing fast if `opencode --version` mismatches the pin. Mirrors
   `install-codex`'s determinism contract.
2. `scripts/write_opencode_config.sh` `[new]` — emits a role-specific
   opencode JSON config to a caller-supplied path. Flags: `--role
   reviewer|writer`, `--model <slug>`, `--project-path`, `--config-path`,
   `--serena on|off`. Writes: `provider.openrouter` (baseURL
   `https://openrouter.ai/api/v1`, `apiKey` from `OPENROUTER_API_KEY` env
   reference), `small_model` pinned to the invoked model (no third-party
   title model), `permission` per role (Q5:A / F3:A), `tools` disabling
   the subagent/task and webfetch tools for reviewers (parity with Codex's
   tool surface), `mcp.serena` when `--serena on`, and
   `provider.openrouter.models.<slug>` context-window limits sourced from
   `scripts/codex_model_catalog.json` (read-only reuse; the catalog file
   is not modified). Atomic write, `::error::` on missing required args —
   same behavior contract as `write_codex_config.sh`.
3. `scripts/opencode_helpers.sh` `[new]` — sourced helpers:
   `opencode_strip_ansi` (filter for captured stdout so the
   `File:`/`Problem:` marker greps keep matching), `opencode_run_cmd`
   (builds the argv array from role/model/variant/config so all call sites
   share one construction), `opencode_emit_failure_alert` (Telegram ERROR
   via `tg_helpers.sh` with stable log prefix `opencode_agent_failure`,
   including phase, role, model, rc, and failure class), and
   `opencode_require_bootstrap` (hard-fail + alert when the binary,
   version pin, or config writer is absent — F4:B).
4. `.github/workflows/opencode-live-smoke.yml` `[new]` — `workflow_dispatch`
   (inputs: optional model filter, `alert_msg_level`). For each roster slug
   plus `MODEL_EDITOR` and `MODEL_EDITOR_FALLBACK`: one cheap
   `opencode run` call ("Return exactly OK") through the generated config,
   asserting rc=0, non-empty ANSI-stripped output, and — from
   `--print-logs` output — that the request completed against the intended
   model; a second identical call asserts the response arrives (prefix-cache
   plumbing sanity). Reports a per-slug pass/fail table in the job summary;
   any failure fails the job. Secrets: `OPENROUTER_API_KEY` only.
5. Tests `[new]`: `tests/test_write_opencode_config.py` (JSON shape per
   role, small_model pinning, permission blocks, catalog-derived limits),
   `tests/test_opencode_helpers.py` (ANSI strip idempotence, argv
   construction incl. `--title` fixed value and stdin contract, bootstrap
   hard-fail path, alert emission shape). Wire both into `ci.yml`'s
   existing test step list.
6. Docs: README env-var rows (`OPENCODE_VERSION`; note that `CODEX_VERSION`
   remains for the other workflows), agents.md — models section note,
   stable log prefix `opencode_agent_failure` registered in the
   "Stable log prefixes" section, `docs/scripts-pending-removal.md`
   entries (see §18.F below), `changelog.d/<pr>-opencode-plumbing.md`.

### Phase 2 — read-side cutover

7. `scripts/review_run_reviewers.sh` — replace the `reviewer_codex_cmd`
   array construction (single site, ~line 3793) with
   `opencode_run_cmd reviewer "${effective_model}" "${attempt_reasoning}"`;
   keep the stall-guard/heartbeat wrapping and stdin feed unchanged; add
   `--print-logs --log-level INFO` so stderr feeds the activity file.
8. Same file — `reviewer_prepare_reasoning_configs` becomes a no-op for
   reasoning (the effort now rides `--variant`); per-reviewer
   `CODEX_HOME` copy is replaced by `write_opencode_config.sh --role
   reviewer` into the slot's runtime dir; `is_mcp_incompatible_model` /
   `strip_all_mcp_server_blocks` are retained but short-circuited with a
   comment (dead on the opencode path; §6 forbids removal).
9. Same file — capacity/cache probe (~lines 480–525) runs its two probe
   calls via `opencode_run_cmd` so the primed prefix matches the
   opencode-shaped requests; captured outputs pass through
   `opencode_strip_ansi` before the `File:`/`Problem:` and
   `CACHE_PROBE_OK` greps; on any slot's terminal failure call
   `opencode_emit_failure_alert` (Q6:B).
10. `scripts/summarize_reviewer_consensus.sh` — swap its codex invocation
    and config sed-patching for `opencode_run_cmd` + a per-call config;
    retry/backoff structure unchanged.
11. `.github/workflows/review_autofix.yml` — add the `install-opencode`
    step (Codex install remains for the still-Codex write side); add
    `opencode_helpers.sh` and `write_opencode_config.sh` to
    `REQUIRED_BOOTSTRAP_SCRIPTS` staging.
12. Test updates: `test_review_autofix_reasoning_schedule.py` (schedule now
    asserts `--variant` argv), `test_review_reviewer_attempt_prompt_isolation.py`
    (stdin path unchanged — assert no regression),
    `test_summarize_reviewer_consensus_sandbox_pin.py` (pin assertion moves
    to the opencode config), `test_review_autofix_review_pipeline_contract.py`.
    `changelog.d/<pr>-opencode-read-side.md`.

### Phase 3 — write-side cutover

13. `scripts/review_apply_fixes.sh` — editor invocation via
    `opencode_run_cmd writer`; the final-attempt `MODEL_EDITOR_FALLBACK`
    switch swaps the `-m` slug exactly as it swaps `--model` today; the
    stderr-FIFO drain logic (`EDITOR_DRAIN_GRACE_SECS`) is re-verified
    against opencode's process tree and its bound kept.
14. `scripts/review_rb_judge.sh` — judge calls run `--role reviewer`
    (read-only posture), judge-fix retries run `--role writer`; retry
    wrappers unchanged.
15. `scripts/review_consolidate.sh` and `scripts/review_conflict_resolve.sh`
    — swap invocations to `opencode_run_cmd writer`; the resolver's
    allowlist, `check_resolver_diff.sh`, fingerprint verification,
    per-attempt snapshot/reflexion loop, and `[ai-merge-resolve]` commit
    contract are untouched (they gate outputs, not the CLI).
16. Prompts: neutralise Codex-specific wording in review-phase prompt
    templates only (apply_patch references → "your file-editing tools";
    the two `You are Codex v0.114.0` lines are in validate prompts, out of
    scope). Audit `prompts/mode-review*.txt`, `prompts/conflict-resolver*.txt`
    for tool-name assumptions.
17. `.github/workflows/review_autofix.yml` — remove the Codex install step
    and any `write_codex_config.sh` call from this workflow only;
    `CODEX_THREAD_REUSE_ENABLED` handling routes to its documented
    fail-open (fresh full prompt) with a log line when set.
18. Test updates: `test_editor_capacity_fallback_contract.py`,
    `test_review_conflict_resolve_reasoning_step_down.py`,
    `test_review_conflict_resolve_retry_state.py`,
    `test_review_pipeline_integration.py`, `test_detect_editor_changes_lost.py`,
    `test_codex_skip_git_repo_check_contract.py` (scope its assertion to
    the workflows still on Codex). `changelog.d/<pr>-opencode-write-side.md`.

## Files & Modules

- `.github/actions/install-opencode/action.yml` `[new]`
- `scripts/write_opencode_config.sh` `[new]`
- `scripts/opencode_helpers.sh` `[new]`
- `.github/workflows/opencode-live-smoke.yml` `[new]`
- `tests/test_write_opencode_config.py` `[new]`
- `tests/test_opencode_helpers.py` `[new]`
- `scripts/review_run_reviewers.sh`
- `scripts/summarize_reviewer_consensus.sh`
- `scripts/review_apply_fixes.sh`
- `scripts/review_rb_judge.sh`
- `scripts/review_consolidate.sh`
- `scripts/review_conflict_resolve.sh`
- `.github/workflows/review_autofix.yml`
- `.github/workflows/ci.yml` (test wiring)
- `prompts/mode-review*.txt`, `prompts/conflict-resolver*.txt` (wording only)
- `README.md`, `agents.md`, `docs/scripts-pending-removal.md`
- `changelog.d/` — one fragment per phase `[new]`
- Existing tests updated per steps 12 / 18

No files are deleted. `install-codex`, `write_codex_config.sh`,
`codex_model_catalog.json`, and all `CODEX_*` identifiers remain (§6).

## Data Model / Index Changes

None. No MongoDB collections, indexes, or `/db/contracts/*` are touched.

## Tests

- **Unit:** new tests for the config writer and helpers (P1); updated
  contract tests per phase (steps 12, 18). All run in `ci.yml`.
- **Live smoke (dispatch):** `opencode-live-smoke.yml` proves per-vendor
  acceptance, reasoning delivery, and cache plumbing with a real key —
  MUST pass before P2 and again before P3 merges (orchestrator judges
  record the run URL in the phase PRs).
- **e2e:** the existing release gate (`test-and-mark-stable.yml` smoke
  phases, including the review/editor bait test and conflict-resolver
  path) runs unmodified and is the merge gate for P2/P3.
- **Production criterion (Q3: A):** after P3 is on `main`, 3 consecutive
  real review_autofix runs with `REVIEWERS_SUCCESSFUL=6/6`, no new
  failure classes in the run ledger, and latency + cache-read telemetry
  within ~20% of the pre-cutover baseline (baseline: the last 3 Codex
  runs before P2 merges, captured in the P2 PR description).

## Risks & Mitigations

- **Reasoning-effort delivery over chat/completions is unconfirmed** (the
  sandbox capture showed a nonstandard `reasoningEffort` key) —
  ACCEPTED — pending the P1 live smoke; if effort is not honored, the fix
  lands in `write_opencode_config.sh` provider options before P2.
- **Vendor-side behavior differences on chat/completions vs Responses API**
  (editor quality, verbosity — opencode has no `model_verbosity` knob) —
  ACCEPTED — pending the 3-run production criterion; rollback is the
  phase revert.
- **Prompt-cache hit-rate change** (opencode's own system prompt + tool
  definitions alter the static prefix) — mitigated by the cache probe
  running through opencode (step 9) and the ±20% telemetry criterion.
- **No OS-level sandbox in opencode** — mitigated by Q5:A/F3:A permission
  postures, ephemeral runners, and the unchanged write-guard/resolver
  allowlist boundaries; reviewers additionally get `edit: deny`, which is
  stricter than today's OS read-only for file edits.
- **Full-prompt leakage to a third-party title model** — eliminated by the
  fixed `--title` argument and `small_model` pinned to the invoked model;
  asserted by unit test.
- **opencode 1.x release churn** — exact-version pin via
  `OPENCODE_VERSION` with npm cache, same determinism contract as
  `install-codex`; bumps go through the live smoke first.
- **Stall-guard heartbeat starvation** if opencode buffers stderr —
  mitigated by `--print-logs --log-level INFO` (continuous stderr) and
  covered by the existing stall-guard tests; the guard's kill path is
  CLI-agnostic.
- **Out-of-DAG-order merge of P2/P3** — prevented by orchestrator
  dependency edges (F6:A); defense-in-depth: `opencode_require_bootstrap`
  hard-fails with an admin alert (F4:B) instead of running a half-wired
  workflow.
- **Consumer repos receive the cutover on sync** — gated by F2:A: `@stable`
  is tagged only after the production criterion passes (see Rollout).

## Rollout

1. Orchestrator implements P1 → (P2 ∥ P3) per the dependency DAG.
2. Before P2 merge: dispatch `opencode-live-smoke.yml` (all slugs green).
3. P2 merges → this repo's `main` runs review with opencode reviewers +
   summariser, Codex writers. Before P3 merge: re-dispatch the smoke for
   the editor slugs.
4. P3 merges → full cutover on `main`. Codex no longer installs in
   review_autofix.
5. Hold `@stable`: do not run the mark-stable/release flow until the Q3:A
   criterion passes (3 consecutive runs, 6/6, no new failure classes,
   telemetry within ~20%). The release gate's own smoke tests must also be
   green — both conditions are required.
6. Tag `@stable` → the 13 consumer repos in
   `.github/ai/consumer_repos.json` cut over on their next
   `update_workflows.yml` sync (daily cron or `repository_dispatch`).
7. Rollback at any point: revert the offending phase PR on `main` (P3's
   revert restores the Codex install step it removed). If a bad state
   reached `@stable`, re-tag `@stable` to the prior release per the
   existing release process.

§18.F registry entries (added in P1, live in
`docs/scripts-pending-removal.md`): `opencode-live-smoke.yml` — type
`long-running`, removal trigger "permanent — review annually", preflight
"no workflow references opencode any more"; `scripts/opencode_helpers.sh`
and `scripts/write_opencode_config.sh` — same trigger, preflight "zero
invocation sites in scripts/ and .github/workflows/"; owner
`shubhodeep1`.

## References

- PR #1704 (Codex v0.114.0 → v0.125.0 bump), PR #1717 (strip-MCP
  workaround), PR #1729 (revert to v0.114.0), PR #1742 (empty-output
  fallout), PR #1752 (apply_patch `function` catalog fix), PR #2210
  (flag-position contract), PR #2185 / #2669 (install determinism/caching)
- `.github/actions/install-codex/action.yml` — determinism contract mirrored
  by `install-opencode`
- README "Prompt Caching (OpenRouter + Codex)" — prefix-cache discipline the
  cutover must preserve
- agents.md "Models in use", "Stable log prefixes", "Review pipeline
  consolidator + ledger contract"
- opencode: https://opencode.ai/docs (config schema at
  https://opencode.ai/config.json), npm package `opencode-ai`
- Session research (2026-08-26): wire-capture comparison of Codex
  0.114.0 / 0.149.1 / opencode 1.18.23 against OpenRouter-shaped payloads
