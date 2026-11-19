# Opencode cutover for clarify

## Summary

Cut the clarify phase (`.github/workflows/clarify.yml`) over from the Codex
CLI to the OpenCode CLI, reusing the plumbing shipped by the review_autofix
cutover (issue #3845). Two phases: P1 stages the opencode helpers and install
step into clarify inert; P2 swaps the single model invocation and removes the
Codex install from `clarify.yml` only.

## §18 Automation surface (plan output requirements, CLAUDE.md §18.E)

- **Scripts:** no new scripts; the change only modifies the existing
  reusable workflow `.github/workflows/clarify.yml` and reuses the existing
  `scripts/opencode_helpers.sh`, `scripts/write_opencode_config.sh`, and
  `.github/actions/install-opencode`.
- **Scheduler / trigger entry point:** the existing clarify triggers —
  `internal-clarify.yml` (source repo) and the consumer wrapper
  `workflow-templates/ai-clarify.yml`, both of which call the reusable
  `clarify.yml` and need no edits. Nothing new to wire.
- **Supervisor:** none required; clarify remains a per-issue one-shot run.
- **DB operations:** none. No MongoDB collections, indexes, or contracts
  are touched (§10 not applicable).
- **§18.F registry:** no new entries. `opencode-live-smoke.yml`,
  `scripts/opencode_helpers.sh`, and `scripts/write_opencode_config.sh`
  already carry entries in `docs/scripts-pending-removal.md` from the
  review cutover; their triggers/preflights are unchanged by this plan.

## Context

Issue #3845 cut `review_autofix.yml` over to opencode: `1.18.23` pinned via
`OPENCODE_VERSION`, installed by `.github/actions/install-opencode`, invoked
through `opencode_run_cmd` (`scripts/opencode_helpers.sh`) with per-role
configs from `scripts/write_opencode_config.sh`, failure-alerted under the
contractual `opencode_agent_failure` log prefix (agents.md "Stable log
prefixes"), and gated by the dispatchable `opencode-live-smoke.yml`. Every
other production phase remains Codex-backed (agents.md "Models in use").

Clarify is the natural next cut: it is the entry phase of every issue
pipeline (high run frequency → fast production signal), performs a single
LLM call, writes no code, and its model slug `openai/gpt-5.6-sol` is already
exercised daily under opencode as the review_autofix editor.

Current clarify implementation (all line refs at the `main` tip as of
2026-08-31):

- `clarify.yml:36` — `MODEL_EDITOR: vars.WORKFLOW_EDITOR_MODEL ||
  'openai/gpt-5.6-sol'`; `clarify.yml:41` — `MODEL_REASONING_EFFORT:
  vars.THINKING_LEVEL_CLARIFY || 'xhigh'` (smoke override to `low` at
  `clarify.yml:447-448`).
- `clarify.yml:160-163` — `Install Codex CLI` step
  (`install-codex@stable`, `CODEX_VERSION || v0.114.0`).
- `clarify.yml:222` — staged-scripts list includes
  `write_codex_config.sh` and `codex_helpers.sh`;
  `clarify.yml:256-271` stages `codex_model_catalog.json`.
- `clarify.yml:676-680` — `codex_config_assemble "${MODEL_EDITOR}"
  "${MODEL_REASONING_EFFORT}" "low"`.
- `clarify.yml:755` (`id: run_codex`) — 3-attempt retry loop; each attempt
  pipes `CODEX_PROMPT_FILE` via stdin to
  `codex --ask-for-approval never -c model_verbosity=low
  -c include_apply_patch_tool=true exec --skip-git-repo-check
  --model "${MODEL_EDITOR}" --sandbox danger-full-access`
  (`clarify.yml:945`), writing `CODEX_OUTPUT_FILE` and retrying on empty
  output.
- Semantic cache (`SEMANTIC_CACHE_BACKEND`) can skip the model call
  entirely on a cache hit; orchestrator-managed issues take a
  `skip_codex=true` fast path with no model call at all.
- No stall-guard sidecar and no capacity-fallback model
  (`WORKFLOW_EDITOR_FALLBACK_MODEL` applies to plan / implement /
  review editor loops, not clarify).

## Decisions

### D1 — Scope: `clarify.yml` only

- **Chosen:** cut over `clarify.yml` alone; `orchestrate_clarify_respond.yml`
  (the clarify-respond twin) stays Codex-backed for a later plan.
- **Alternatives considered:** including clarify-respond in this plan.
- **Why:** user answer `Q1: A` — smallest blast radius, one pipeline at a
  time, mirroring the review cutover pattern.

### D2 — Permission posture: `writer` role

- **Chosen:** invoke opencode with the existing `writer` role (full tool
  allow), exact parity with today's `--sandbox danger-full-access`.
- **Alternatives considered:** `reviewer` role (denies bash/edit/webfetch —
  a behavior change that removes web search and shell exploration); a new
  `clarify` agent in `write_opencode_config.sh` (truest posture, but new
  identifiers and test surface).
- **Why:** user answer `Q2: A` — zero changes to
  `write_opencode_config.sh`, no new identifiers, parity with production
  behavior today.

### D3 — Production criterion: aggregate, recorded before tagging

- **Chosen:** hold `@stable` until ≥10 real post-cutover clarify runs on
  `main` show ≥90% success and median wall-clock within ~25% of the
  pre-cutover baseline; evidence recorded in this plan doc before the
  release is dispatched.
- **Alternatives considered:** the review plan's strict
  3-consecutive-runs-each-in-band criterion; no hold at all.
- **Why:** user answer `Q3: A`. The #3845 postscript showed the strict
  per-run criterion is unsatisfiable in practice (PR size drives per-run
  latency far more than the runtime does) and was ultimately bypassed;
  an aggregate criterion measures the same health without the trap.

### D4 — Two phases

- **Chosen:** P1 inert plumbing, P2 cutover.
- **Alternatives considered:** a single-phase swap (plumbing already
  exists repo-wide).
- **Why:** user answer `Q4: A` — each phase independently mergeable and
  production-safe; P1 de-risks staging/bootstrap on the live path before
  any behavior changes.

### D5 — Smoke gate before P2

- **Chosen:** dispatch `opencode-live-smoke.yml` with
  `model_filter=openai/gpt-5.6-sol` and record the green run URL in the
  P2 PR before it merges.
- **Alternatives considered:** skipping the smoke (slug already proven in
  review production).
- **Why:** user answer `Q5: A` — free insurance, same gate the review
  cutover used.

## Goals

- `clarify.yml` runs its clarification call through opencode
  (`openrouter/openai/gpt-5.6-sol` via `opencode_run_cmd`), with the Codex
  CLI no longer installed or invoked by that workflow.
- Behavior parity: same model slug and reasoning-effort resolution
  (`WORKFLOW_EDITOR_MODEL`, `THINKING_LEVEL_CLARIFY`, smoke override to
  `low`), same 3-attempt/empty-output retry loop, same prompt assembly,
  same semantic-cache and `skip_codex` fast paths, same output file
  contract for every downstream step.
- Failures surface under the existing `opencode_agent_failure` stable log
  prefix with `phase=clarify_run`.
- Consumer repos receive the cutover only via the normal `@stable` release
  after the D3 criterion is recorded as met.
- Every Codex-named identifier reachable from clarify (env vars
  `CODEX_VERSION`, `CODEX_PROMPT_FILE`, `CODEX_OUTPUT_FILE`, step id
  `run_codex`, output `skip_codex`, `RUNTIME_DIR=/tmp/codex-issue-*`,
  `.codex-workflow-src` staging dirs) keeps its name (§6).

## Non-goals

- No cutover of `orchestrate_clarify_respond.yml` (D1) or of any other
  Codex-backed phase (plan, implement, orchestrate/judge, validate,
  memory_maintenance, security-audit, check_failure_triage,
  workflow-log-analysis).
- No change to `write_opencode_config.sh` roles or permissions (D2).
- No removal of `install-codex`, `write_codex_config.sh`,
  `codex_helpers.sh`, or `codex_model_catalog.json` from the repo — other
  workflows still use them; clarify merely stops staging/invoking them.
- No new release-gate automation; the D3 hold is procedural, enforced by
  recording evidence in this doc before dispatching the release.
- No semantic-cache, prompt-assembly, memory, or label-flow changes.

## Constraints

- **§6 naming immutability** — all `CODEX_*`/`codex`-named identifiers in
  clarify keep their names even where they now carry opencode artifacts
  (P3 of #3845 set the precedent: the review workflow's `codex-agent` job
  name and `Run Codex resolver` step name survive the cutover unchanged).
  New names introduced here must not collide with any in-scope identifier.
- **§10 MongoDB** — not applicable; no collections touched.
- **§14 consumer repos** — `clarify.yml` is a reusable workflow consumed
  by all 13 repos in `.github/ai/consumer_repos.json` through
  `ai-clarify.yml` wrappers pinned to `@stable`. Wrappers need no edits;
  propagation happens at `@stable` tag time, gated by D3. Consumer secrets
  are unchanged (`OPENROUTER_API_KEY` already required).
- **§15 GitHub API hygiene** — this plan adds zero new `gh`/API calls to
  any workflow or script. The rollout baseline reconstruction reads
  existing Actions run metadata once, interactively.
- **§20 changelog** — each phase PR ships one fragment
  (`changelog.d/<issue>-opencode-clarify-p1.md`, `…-p2.md`); P2's is the
  operator-visible entry (runtime change).
- **agents.md / README** — the "Models in use" note ("Other production
  phases remain Codex-backed…") and the README's `OPENCODE_VERSION` /
  `CODEX_VERSION` variable descriptions must be updated in P2, in the same
  PR (§7).

## Approach

Mirror the review_autofix cutover mechanics at clarify's much smaller
scale. P1 makes the opencode toolchain present-but-unused on the clarify
path (install step + staged helpers + config writer), proving bootstrap and
staging work on this workflow with zero behavior change. P2 swaps the body
of the `run_codex` step from the `codex exec` pipeline to
`write_opencode_config.sh --role writer` + `opencode_run_cmd`, keeps the
surrounding retry/empty-output/telemetry structure intact, and removes the
Codex install and codex-only staging entries from `clarify.yml` alone.

The invocation swap inside the retry loop, per attempt:

```
config="${RUNTIME_DIR}/opencode-clarify.json"
scripts/write_opencode_config.sh --role writer \
  --model "${MODEL_EDITOR}" --out "${config}"   # once, before the loop
cat "${CODEX_PROMPT_FILE}" | opencode_run_cmd \
  writer "${MODEL_EDITOR}" "${MODEL_REASONING_EFFORT}" \
  "${config}" "${GITHUB_WORKSPACE}" \
  > "${CODEX_OUTPUT_FILE}" 2> >(tee -a "${RUNTIME_DIR}/codex_log.txt" >&2)
```

(`write_opencode_config.sh`'s exact flag surface is authoritative — the
implementer must match its usage string; `opencode_run_cmd` already pins
`openrouter/` provider, `--print-logs --log-level INFO`, `NO_COLOR=1`, and
`--auto` for the writer role.) `MODEL_REASONING_EFFORT` values (`xhigh` /
`high` / `medium` / `low`) map 1:1 onto `opencode_run_cmd`'s accepted
variants, including the smoke-test `low` override. On final-attempt
failure, `opencode_emit_failure_alert` fires with `phase=clarify_run
role=writer`, then the existing failure handling proceeds unchanged.
`opencode_strip_ansi` runs over `CODEX_OUTPUT_FILE` defensively before
downstream parsing (stdout should already be clean under `NO_COLOR=1`).

## Phases & Merge Strategy

Executed by the AI orchestrator, one PR per phase, each production-safe at
merge.

1. **P1 — inert opencode plumbing in clarify** (priority 1)
   - **Scope:** add the `Install OpenCode CLI` step (uses
     `install-opencode`, `opencode_version: OPENCODE_VERSION || 1.18.23`)
     alongside the existing `Install Codex CLI` step; add
     `opencode_helpers.sh` and `write_opencode_config.sh` to
     `clarify.yml`'s staged-scripts list (`clarify.yml:222`); add the P1
     contract test. No invocation change — Codex still runs clarify.
   - **Files:** `.github/workflows/clarify.yml`,
     `tests/test_clarify_opencode_contract.py` [new],
     `changelog.d/<issue>-opencode-clarify-p1.md` [new].
   - **Done:** CI green; a real clarify run on `main` behaves identically
     (Codex invoked, opencode installed but unused); contract test pins
     the staging list and install step.
   - **Rollback:** revert the P1 PR — clarify never referenced the staged
     opencode files, so the revert is inert too.

2. **P2 — cutover: clarify runs on opencode** (priority 2; merges only
   after P1 is on `main` and the D5 smoke run is green)
   - **Scope:** replace the `codex exec` invocation inside `run_codex`
     with the opencode pipeline (see Approach); add
     `opencode_require_bootstrap` before first use; emit
     `opencode_agent_failure` on terminal failure; remove the
     `Install Codex CLI` step, the `write_codex_config.sh` /
     `codex_helpers.sh` staging entries, the `codex_model_catalog.json`
     staging block, and the `codex_config_assemble` call from
     `clarify.yml` only; update the summary-table "Model" row source if
     needed; update agents.md + README rows; update the P1 contract test
     to assert the Codex paths are gone and the opencode invocation is
     present.
   - **Files:** `.github/workflows/clarify.yml`,
     `tests/test_clarify_opencode_contract.py`, `agents.md`, `README.md`,
     `changelog.d/<issue>-opencode-clarify-p2.md` [new].
   - **Done:** CI green; the `test-and-mark-stable.yml` e2e smoke's
     clarify phase (its Phase 1 wait) passes on a dispatch; a real
     clarify run on `main` posts a well-formed questions comment via
     opencode.
   - **Rollback:** revert the P2 PR — this restores the Codex install
     step and invocation it removed (the review P3 revert contract).

Ordering note: P2 depends on P1 (dependency edge for the orchestrator).
Each phase is still independently *safe*: P1 alone changes no behavior;
P2's PR contains the complete cutover, so `main` is never in a half-wired
state. `opencode_require_bootstrap` hard-fails with an alert if P2 ever
runs without its staged helpers (same defense-in-depth as the review
cutover's out-of-order guard).

## Implementation Steps

**Phase P1**

1. `.github/workflows/clarify.yml` (~line 163): add an
   `Install OpenCode CLI` step directly after `Install Codex CLI`, using
   `shubhodeep1/coding-workflows/.github/actions/install-opencode` pinned
   the same way review_autofix pins it, with
   `opencode_version: ${{ vars.OPENCODE_VERSION || '1.18.23' }}` and an
   `OPENCODE_VERSION` env default added to the workflow env block
   (matching `review_autofix.yml:97`).
2. `.github/workflows/clarify.yml` (line 222): append
   `opencode_helpers.sh write_opencode_config.sh` to the staged-scripts
   loop list.
3. `tests/test_clarify_opencode_contract.py` [new]: assert (a) the staged
   list contains both opencode scripts, (b) the install-opencode step
   exists with the version pin, (c) the `codex exec` invocation is still
   present in P1 (this assertion flips in P2). Model the test on
   `tests/test_opencode_live_smoke_workflow.py`'s
   read-the-workflow-source style.
4. `changelog.d/<issue>-opencode-clarify-p1.md` [new] (`<!-- changelog:
   added -->`, contributor-slanted: inert staging only).

**Phase P2**

5. `.github/workflows/clarify.yml` (~line 676): replace
   `codex_config_assemble …` with sourcing `opencode_helpers.sh`,
   `opencode_require_bootstrap`, and one `write_opencode_config.sh
   --role writer --model "${MODEL_EDITOR}"` call writing into
   `RUNTIME_DIR`.
6. `.github/workflows/clarify.yml` (`run_codex` step body, ~line 945):
   swap the per-attempt `codex … exec …` command for the
   `opencode_run_cmd writer …` pipeline per Approach; keep the 3-attempt
   loop, the empty-output retry, `CODEX_PROMPT_FILE` stdin feed,
   `CODEX_OUTPUT_FILE` capture, and the `codex_log.txt` stderr tee
   exactly as they are; run `opencode_strip_ansi` on the output file
   after a successful attempt; on final failure call
   `opencode_emit_failure_alert` with `phase=clarify_run role=writer`
   before the existing failure path.
7. `.github/workflows/clarify.yml`: delete the `Install Codex CLI` step
   (lines 160-163), the `write_codex_config.sh` / `codex_helpers.sh`
   entries from the staged list, and the `codex_model_catalog.json`
   staging block (lines 256-271). All deletions are clarify-local; the
   files themselves remain in the repo for other workflows.
8. `tests/test_clarify_opencode_contract.py`: flip the P1 assertions —
   `must_not_contain` `install-codex` / `codex exec` in `clarify.yml`;
   `must_contain` `opencode_run_cmd`, `opencode_require_bootstrap`, and
   the failure-alert call.
9. `agents.md`: update the "Models in use" opencode paragraph to name
   clarify as opencode-backed; keep the historical Phase-1 comment block
   untouched (it is a fingerprint anchor).
10. `README.md`: update the `OPENCODE_VERSION` and `CODEX_VERSION`
    variable rows ("used by review_autofix and clarify" / "production
    paths outside review_autofix and clarify"), and the OpenCode
    prompt-caching/live-smoke paragraphs where they claim review-only
    scope.
11. `changelog.d/<issue>-opencode-clarify-p2.md` [new] (`<!-- changelog:
    changed -->`, operator-facing headline: clarify now runs on
    OpenCode; Codex no longer installed in that workflow).

## Files & Modules

- `.github/workflows/clarify.yml` — P1 + P2 edits (only workflow touched)
- `tests/test_clarify_opencode_contract.py` [new]
- `agents.md` — P2 doc row updates
- `README.md` — P2 doc row updates
- `changelog.d/<issue>-opencode-clarify-p1.md` [new]
- `changelog.d/<issue>-opencode-clarify-p2.md` [new]

## Data Model / Index Changes

None (§10 not applicable).

## Tests

- **Unit / contract:** the new
  `tests/test_clarify_opencode_contract.py` pins the staging list,
  install step, and (post-P2) the absence of Codex paths in
  `clarify.yml`; existing `tests/test_opencode_helpers.py` and
  `tests/test_write_opencode_config.py` already cover the shared helpers
  and need no changes (D2 keeps roles untouched). Wrapper-predicate
  parity is unaffected (`tests/test_phase_wrapper_predicate_contract.py`
  — no `if` predicates change).
- **Lint:** `ci.yml` yamllint / shellcheck / ruff run as-is.
- **Live smoke (dispatch):** `opencode-live-smoke.yml` with
  `model_filter=openai/gpt-5.6-sol` must be green before P2 merges; the
  run URL is recorded in the P2 PR body (D5).
- **e2e:** the `test-and-mark-stable.yml` e2e smoke drives a real issue
  through clarify (its Phase 1 wait) and is the release gate; it runs
  unmodified.

## Risks & Mitigations

- **Web-search semantics differ** (Codex `web_search` tool vs opencode's
  `webfetch` under the writer role) — ACCEPTED — question quality is
  monitored by the D3 criterion; the clarify prompt's web-usage guidance
  is tool-agnostic.
- **Writer role grants edit/bash to a phase that shouldn't write** —
  ACCEPTED — exact parity with today's `danger-full-access` Codex call
  (`Q2: A`); clarify commits nothing and pushes nothing; a tighter
  `clarify` agent remains available as a follow-up.
- **Announce-without-emit (openai/codex#11151 class)** — not applicable:
  clarify's output is plain text, no patch emission; opencode does not
  use `include_apply_patch_tool`.
- **Prompt-cache hit-rate change** (opencode's system prompt alters the
  static prefix) — ACCEPTED — pending the D3 evidence window; duration is
  the observable proxy, as in the review cutover.
- **No stall guard on clarify** — ACCEPTED — status quo today (no
  `codex_heartbeat.sh` on this path); the 3-attempt loop plus job timeout
  bound the damage, and `--print-logs` keeps stderr live.
- **Hold is procedural, not mechanical** — ACCEPTED — per `Q3: A` the
  evidence is recorded in this doc before the release dispatch; the
  #3845 postscript documents why a mechanical gate was not added here,
  and adding one remains open follow-up work outside this plan's scope.
- **Consumer breakage on sync** — mitigated: wrappers and secrets are
  unchanged; consumers receive the cutover only after the D3 criterion
  passes and `@stable` is tagged; rollback is re-tagging `@stable` to the
  prior release.

## Rollout

1. Orchestrator lands P1; verify one real clarify run on `main` is
   unchanged (Codex ran, opencode installed).
2. Dispatch `opencode-live-smoke.yml` with
   `model_filter=openai/gpt-5.6-sol`; record the green run URL in the P2
   PR (D5).
3. Orchestrator lands P2 → clarify on `main` runs on opencode. Codex no
   longer installs in `clarify.yml`.
4. **Hold `@stable`.** Reconstruct the pre-cutover baseline from Actions
   run metadata: the last 10 real pre-cutover clarify runs where the
   model call actually executed (exclude `skip_codex` fast paths,
   semantic-cache hits, and cancelled runs); record run IDs and median
   wall-clock below.
5. Collect ≥10 real post-cutover clarify runs on `main` under the same
   exclusions. Criterion (D3): ≥90% success AND median wall-clock within
   ~25% of the baseline median AND no new failure classes (only
   `opencode_agent_failure … failure_class=<existing classes>` at worst).
   Record the evidence table below **before** dispatching any
   mark-stable release.
6. Tag `@stable` → the 13 consumer repos cut over on their next
   `update_workflows.yml` sync / `repository_dispatch`.
7. Rollback at any point: revert the offending phase PR on `main` (P2's
   revert restores the Codex install and invocation). If a bad state
   reached `@stable`, re-tag `@stable` to the prior release.

### Production criterion evidence (fill before tagging `@stable`)

| Leg | Baseline (pre-cutover) | Post-cutover | Verdict |
|---|---|---|---|
| Run set (IDs) | pending | pending | — |
| Success rate | — | pending (≥90% required) | pending |
| Median wall-clock | pending | pending (within ~25%) | pending |
| New failure classes | — | pending (none required) | pending |

## References

- Issue #3845 — Opencode cutover for review_autofix (tracking; closed)
- `docs/plans/opencode-review-autofix-cutover-plan.md` — predecessor plan,
  including the recorded parity-evidence postscript this plan's D3
  criterion learns from
- PR #3868 — precedent for installing OpenCode alongside Codex in a live
  workflow (review_autofix warm-cache install)
- `scripts/opencode_helpers.sh`, `scripts/write_opencode_config.sh`,
  `.github/actions/install-opencode`, `.github/workflows/opencode-live-smoke.yml`
  — shared plumbing reused here
