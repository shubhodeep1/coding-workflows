# Orchestrator validation resilience

## Archived status

This file is the canonical completed-plan record for tracking issue `#2934`.

The closeout summary below reflects the shipped repository state audited for archival. The historical plan text that follows is preserved for context; where the original plan used draft placeholder names later finalized differently in code, the closeout summary is the authoritative record of what shipped. No acceptance criteria were silently de-scoped in this archive.

## Closeout summary

- **AC1:** A dedicated `node-runtime` validation family shipped under `workflow-templates/validation-harness/node-runtime/`, with the fixture/docs/tests needed for non-Hardhat Node consumers and finalized JSON-based custom/skip test controls.
- **AC2:** Harness failures are now handled as a distinct validation class in the orchestrator via additive label/state handling (`ai:harness-broken`) without burning the normal validation-recovery budgets; regression coverage lives in the orchestrator and label tests.
- **AC3:** The orchestrator now ensures an eager draft integration-to-default-branch PR exists before validation completes and keeps its validation status section synchronized.
- **AC4:** Per-SHA validation history shipped on the `ai-memory` branch via `validation_history.v1` schema support and is consulted before ready-to-merge promotion.
- **AC5:** Integration-branch staleness alerting shipped with bounded re-alerting and test coverage around the alert/re-alert behavior.
- **AC6:** The operator `ai:force-merge` bypass path shipped with audit recording and documented/operator-tested draft-PR promotion behavior.
- **AC7:** Integration-branch backpressure shipped, preventing new managed-issue merges once the ahead-count threshold is hit until the backlog clears.
- **AC8:** The existing `/revalidate` flow now composes with the new lifecycle by clearing harness-broken state, refreshing eager-PR status, and recording revalidation audit history.

## Evidence snapshot

- Node-runtime implementation and closeout evidence: `workflow-templates/validation-harness/node-runtime/`, `examples/validation-fixtures/node-runtime.yml`, `README.md`, `tests/test_family_node_runtime.py`, `tests/test_render_validation_templates_node_runtime_regressions.py`, `tests/test_validate_workflow_validate_bootstrap.py`
- Orchestrator lifecycle evidence for AC2/AC3/AC5/AC6/AC7/AC8: `scripts/orchestrate_poll_process.sh`, `.github/ai/label_contract.v1.json`, `tests/test_orchestrate_poll_process.py`, `tests/test_ai_labels.py`
- AI-memory evidence for AC4/AC8: `ai-memory/schemas/validation_history.v1.json`, `ai-memory/schemas/revalidate_events.v1.json`, `ai-memory/schemas/operator_bypass_audit.v1.json`, `tests/test_ai_memory_processed_command_entry.py`

## Historical plan

## Summary

Make the orchestrator pipeline resilient to validation failures so commits cannot get "lost" on an integration branch: add a Node-runtime validation family, honor the existing `harness_error` distinction end-to-end, open the integration→main squash PR as a draft as soon as the branch is ahead (not after validation passes), persist per-SHA validation history, escalate stale integration branches via Telegram, add a documented bypass label, add backpressure on the integration branch, and harden the already-shipped `/revalidate` command to compose with the new lifecycle.

## Context

A consumer repo (`shubhodeep1/bitsafe.io`) accumulated 11 commits on its `orchestrator/project-N` integration branch over ~30 hours with no PR open against `main`, because validation kept hitting a harness defect (no Node-runtime family) and the eager squash PR is only created downstream of `ai:validated → ai:ready-to-merge`. The branch was not lost — it is a real ref — but it was invisible: no reviewer signal, no merge path, and a growing 11-commit / 80-file squash backlog.

Two coupled problems:

- **Trigger (small):** `workflow-templates/validation-harness/` has families `_shared`, `node-hardhat-solidity`, `python-mongo-flask`, `python-mongo-repo-checks`, `python-repo-checks`. A non-Hardhat Node consumer has no slot — the Hardhat family unconditionally renders `tests/30_hardhat_test.sh.j2` and its `validate.env.j2` does not preserve `manifest.custom_tests` / `manifest.skip_tests`.
- **Resilience (large):** `.github/workflows/validate.yml` already exits `2` for `harness_error` and `1` for application failure (L1062–1067), but `scripts/orchestrate_poll_process.sh` consumes only the `ai:validation-failed` / `ai:validate-failed` labels (L8405–8417) and treats both classes the same. Recovery budgets (`MAX_VALIDATION_RECOVERY_ATTEMPTS`, `MAX_VALIDATE_CYCLES`) get burned by harness defects that retrying cannot fix. The eager squash PR (`ensure_eager_final_pr`, L2911) only fires post-validation. There is no per-SHA validation history, no staleness alert, and no operator bypass for upstream harness outages.

Investigation against the codebase confirmed every line citation in the source proposal. **One material reframe:** `/revalidate` already exists as a slash command at `scripts/orchestrate_poll_process.sh:8669–8710` and is documented in `README.md:1405–1414`. It resets `validation_cycle`, `validation_recovery_count`, `validation_failure_*` state, removes `ai:validation-failed` / `ai:validate-failed`, re-applies `ai:validating`, and re-dispatches. So Fix 8 collapses from "build /revalidate" to "compose existing /revalidate with the new Fix 2 / Fix 3 / Fix 4 surfaces." See [Approach](#approach) for the revised framing.

Constraints from project rules that bind this work:

- **CLAUDE.md §6 (naming immutability)** — every new label / env var / log key is added alongside the existing ones; no rename of `ai:validation-failed`, `ai:validated`, `ai:ready-to-merge`, `ai:validating`, `ai:validation-recovery`, `MAX_VALIDATION_RECOVERY_ATTEMPTS`, `MAX_VALIDATE_CYCLES`, `validation_recovery_count`, `judge_fingerprint_repeat_count`, `ensure_eager_final_pr`, or `tg_notify`. `agents.md` lists `LABEL_REPAIR*`, `AUTOFIX_*`, `AI_PHASE_FAILURE_V1`, `SEMBLE_*`, `SERENA_*` as contractual log prefixes — new prefixes added by this work join that class.
- **CLAUDE.md §10 (MongoDB) / unattended §12** — no MongoDB collections are touched (the repo has no `/db/` directory); §10 does not directly apply. The two persistent stores in scope are the orchestrator state file (`STATE_FILE`, JSON on disk per tracking issue) and the `ai-memory` git branch (managed via `scripts/memory_helpers.sh` and `scripts/ai_memory.py`).
- **CLAUDE.md §14 (consumer repos)** — `shubhodeep1/bitsafe.io` is already in `.github/ai/consumer_repos.json` (10 listed repos); the changes propagate via the existing `mark-stable.sh` + `repository_dispatch` flow when each PR lands. No consumer wrapper YAML changes.
- **CLAUDE.md §15 / unattended §14 (GitHub API hygiene)** — every new `gh` call must reuse or extend an existing one. The orchestrator already runs `gh run view` / `gh pr list` / `gh pr edit` per cycle for the tracking issue; new data needs (run-output `raw_status`, draft state, mergeability) extend those rather than adding parallel calls.
- **CLAUDE.md §4 / unattended §8 (env-var defaults)** — every new env var ships with a default.
- **CLAUDE.md §9 / unattended §11 (code style)** — tabs for shell/Python; 2-space YAML; opening braces on a new line.

## Goals

- A non-Hardhat Node consumer can declare `type: node-runtime` and run `npm --workspace=apps/api run test` with no Hardhat assets rendered (AC1).
- `harness_error` (exit 2 from `validate.yml`) does not decrement `MAX_VALIDATION_RECOVERY_ATTEMPTS`, `MAX_VALIDATE_CYCLES`, or `judge_fingerprint_repeat_count`; instead applies `ai:harness-broken` alongside `ai:validation-failed` and triggers a harness re-render (AC2).
- Every commit on `orchestrator/project-N` is reachable from an open PR against `main` within one poll cycle, regardless of validation state. Draft → ready on `ai:ready-to-merge`; stays draft on `ai:harness-broken` with a status comment (AC3).
- Per-SHA validation outcomes are persisted on the `ai-memory` branch and consulted before promoting `ai:validated → ai:ready-to-merge` (AC4).
- An integration branch ahead of `main` for >6h fires exactly one Telegram alert; re-alert at most once per 12h (AC5).
- `ai:force-merge` on the tracking issue flips the draft PR to ready within one poll cycle, writes an audit comment, and persists an audit memory record (AC6).
- After 10 commits ahead of `main`, the orchestrator refuses new `ai/issue-N` merges into the integration branch and labels the tracking issue `ai:integration-backpressure` (AC7).
- The existing `/revalidate` command composes with all of the above: clears `ai:harness-broken`, refreshes the Fix 3 draft-PR status section, writes a `revalidate_event.v1` audit memory record (AC8).

## Non-goals

- Building `/revalidate` from scratch — it already exists at `scripts/orchestrate_poll_process.sh:8669–8710`. Fix 8 only adds composition with the new Fix 2 / Fix 3 / Fix 4 surfaces.
- Auto-firing `/revalidate` when the integration-branch SHA changes (operator confirmed "compose-only" scope in Q2).
- Adding a label-based `ai:revalidate-requested` trigger (Q2).
- Renaming or repurposing any existing identifier (CLAUDE.md §6).
- Changing the `node-hardhat-solidity` family — real Hardhat consumers still depend on it.
- Changing consumer wrapper YAML shape — the only way `.github/workflows/ai-*.yml` would need to change is if env vars were renamed, which we do not do.
- Auto-fallback to `node-runtime` when a repo has `package.json` but no `hardhat.config.*` — out of scope (see Open Questions Q-OQ1); the new family is selected only on explicit `manifest.type == "node-runtime"`.
- New MongoDB collections, contracts, or indexes — none touched.

## Constraints

- **§6 naming immutability:** all new labels (`ai:harness-broken`, `ai:force-merge`, `ai:integration-backpressure`), new env vars (`ORCH_INTEGRATION_STALE_ALERT_HOURS`, `ORCH_INTEGRATION_STALE_REALERT_HOURS`, `ORCH_INTEGRATION_MAX_AHEAD_COMMITS`), new state-file keys (`validation_history_*`, `integration_stale_*`, `force_merge_*`, `backpressure_*`), and new log prefixes (`HARNESS_ERROR_DETECTED`, `EAGER_DRAFT_PR_*`, `INTEGRATION_STALE_*`, `FORCE_MERGE_BYPASS`, `BACKPRESSURE_TRIGGERED`) are added alongside existing ones. Nothing existing is renamed or removed.
- **§4 env-var defaults:** Fix 5 `ORCH_INTEGRATION_STALE_ALERT_HOURS=6`, `ORCH_INTEGRATION_STALE_REALERT_HOURS=12`. Fix 7 `ORCH_INTEGRATION_MAX_AHEAD_COMMITS=10`. All overridable per-consumer via repo variables. (See Open Questions Q-OQ2 to confirm these.)
- **§10 contracts:** N/A (no Mongo). The AI-memory branch is the relevant persistence layer; new shapes carry an explicit `schema_version`.
- **§14 consumer repos:** changes propagate via `mark-stable.sh` after each PR merges; no per-consumer wrapper edits required.
- **§15 GitHub API hygiene:** Fix 2 reads `raw_status` from the existing `gh run view --json jobs` call already issued per cycle (Q3 answer); Fix 3 extends the existing `gh pr list` / `gh pr edit` calls rather than adding parallel ones; Fix 7 reuses the existing `git rev-list main..orchestrator/project-N` ahead-count rather than a new `gh api compare` call. New batched helpers document their input/output shape + call count.
- **§9 code style:** shell + Python tab-indented; YAML 2-space; Dockerfile/Jinja templates follow the existing family conventions.
- **Schema versions:** new AI-memory records use `validation_history.v1`, `bypasses.v1`, `revalidate_event.v1`. (See Open Questions Q-OQ3 to confirm these strings.)
- **bias to action vs ask-first:** the unattended pipelines that will run this code follow `unattended_system_instructions.md` (no STOP-and-ASK). All operator-facing affordances surface as comments on the tracking issue / PR plus optional Telegram alerts — never blocking workflow runs.

## Approach

Eight fixes, four PRs (revised sequencing in [Implementation Steps](#implementation-steps)). The grouping is by review cost, not by per-fix dependency: Fix 1 is purely additive and unblocks the consumer; Fix 2 is a focused plumbing change with broad orchestrator surface; Fixes 3 / 4 / 5 / 8 are one coherent lifecycle reshape; Fixes 6 / 7 are guard rails layered onto the new lifecycle.

Key design choices:

- **Fix 2 detection surface (Q3 answer):** the orchestrator reads `raw_status` from the dispatched validation run's outputs via `gh run view --json jobs,outputs <run-id>`, extending the existing per-cycle run lookup. The failure-summary comment scrape is the documented fail-open fallback when the API read fails. The `validation_completed` memory candidate (already written at `validate.yml:824–838`) is not on the critical path because the orchestrator must decide budget impact before any memory write completes.
- **Fix 3 draft state:** open the draft via `gh pr create --draft`; flip to ready via `gh pr ready` (or the GraphQL `markPullRequestReadyForReview` mutation). Today `ensure_eager_final_pr` (`L2911`) does not pass `--draft`; the patch adds it as the default for the new "branch ahead before validation passes" code path and keeps the existing post-validation path emitting a ready PR for backward compatibility (no consumer-side regression on already-validated branches).
- **Fix 4 per-SHA history:** one file per integration branch on the `ai-memory` branch, path `validation_history/<sha-prefix>/<sha>.json`, list of `{outcome, raw_status, run_id, timestamp, schema_version}` entries. Read on every poll cycle and consulted before `ai:validated → ai:ready-to-merge` promotion; promotion requires "≥1 pass and no fail since the most recent integration-branch commit" (where "since" is by `timestamp` and `outcome != harness_error`). Fail-open: AI-memory read failure falls back to the current label-only path so the lifecycle never stalls on memory-branch outages.
- **Fix 5 staleness alerting:** state-file key `integration_stale_last_alerted_at` (unix epoch); poll-cycle check: if `time() - last_main_squash_at > ORCH_INTEGRATION_STALE_ALERT_HOURS*3600` AND `time() - integration_stale_last_alerted_at > ORCH_INTEGRATION_STALE_REALERT_HOURS*3600`, fire `tg_notify` and update the key. Single alert per re-alert window.
- **Fix 6 force-merge bypass:** label-driven, consumes `GITHUB_ACTOR` from the dispatched poll-cycle event metadata (the label-add event isn't available cycle-locally, so we use the actor who added it via `gh api repos/.../issues/{n}/events?event=labeled` — one new call per cycle when the label is present, gated behind label presence). Bypass writes an audit memory record `bypasses.v1` with `confidence: 1.0` and posts a comment on the issue + PR before flipping the PR ready.
- **Fix 7 backpressure:** before each new `ai/issue-N` merge attempt into `orchestrator/project-N`, compute `git rev-list --count main..orchestrator/project-N`; if `>= ORCH_INTEGRATION_MAX_AHEAD_COMMITS`, decline the merge, post a comment linking to the open draft squash PR, label the tracking issue `ai:integration-backpressure`. The label clears automatically when the squash PR merges and the integration branch resets.
- **Fix 8 compose-only:** extend the existing `/revalidate` handler (`L8669–8710`) to also (a) remove `ai:harness-broken` if present, (b) trigger the Fix 3 status-section refresh on the draft PR, (c) write a `revalidate_event.v1` audit memory record. No new trigger surfaces.

Per-PR sequencing (revised from the original A/B/C/D after the Fix 8 reframe):

- **PR A — Fix 1** (`node-runtime` family). Standalone; unblocks bitsafe.io.
- **PR B — Fix 2** (`harness_error` plumbing + `ai:harness-broken` label + harness re-render). Standalone; the highest-value resilience win.
- **PR C — Fixes 3 + 4 + 5 + 8** (lifecycle reshape + history + alerting + revalidate compose). One coherent change; Fix 8 is small enough to fit inside PR C because it's compose-only.
- **PR D — Fixes 6 + 7** (force-merge label + backpressure). Guard rails on top of the new lifecycle.

`scripts/mark-stable.sh` is run after each PR merges so consumers pick up the changes.

## Implementation Steps

Numbered, one logical commit per step where practical. Each step is small enough to land independently within its PR.

### PR A — Fix 1: `node-runtime` validation family

1. **Add `node-runtime` family directory.** Create `workflow-templates/validation-harness/node-runtime/` mirroring `python-repo-checks/`:
   - `Dockerfile.app.j2` — base `FROM node:22-bookworm` (default; see Open Questions Q-OQ4 to confirm Node major), `apt-get` install of `curl ca-certificates jq git`, `WORKDIR /app`, copy package.json + lockfile, `npm ci`, copy rest, `EXPOSE {{ manifest.port | default('3000') }}`, default `CMD` honoring `manifest.entry`. Honor `manifest.health_check` for an explicit `HEALTHCHECK`.
   - `docker-compose.test.yml.j2` — mirror `python-repo-checks/` layout (test container + harness, no separate app container if `manifest.entry` is the test runner itself; spawn an app container only when `manifest.entry` resolves to a server-style entry point per `manifest.type` slot).
   - `validate.env.j2` — emit `APP_SERVICE`, `APP_URL`, `HEALTH_TIMEOUT_SECONDS`, `HEALTH_POLL_INTERVAL_SECONDS`, `TAIL_LINES`, `VALIDATION_TEST_*` defaults, `CANARY_TOOLS` from `slots.canary_tools`, AND **`CUSTOM_TESTS_CMD="{{ manifest.custom_tests | default('') }}"` + `SKIP_TESTS="{{ (manifest.skip_tests | default([])) | join(' ') }}"`** — the omission cited by the bitsafe.io failure.
   - `tests/` — `00_canary.sh.j2`, `10_family_marker.sh.j2`, `20_import_audit.sh.j2`, `90_tap_report.sh.j2`. **No `30_hardhat_test.sh`. No RPC probe.** When `CUSTOM_TESTS_CMD` is set, append a `50_custom_tests.sh.j2` that runs the command and emits TAP. When `SKIP_TESTS` contains a token matching a generated test (e.g. `hardhat`, `solidity`), the `90_tap_report.sh.j2` aggregator omits it. (Files: ~6 new `.j2` files under `workflow-templates/validation-harness/node-runtime/`.)
2. **Register the family in the selector.** `scripts/render_validation_templates.py:95–98`: add `"node-runtime": FamilySpec(name="node-runtime", relative_dir="node-runtime")` to `FAMILY_REGISTRY`. No `RENDERED_OUTPUT_ALIASES` entry needed (no rename of generated test paths). Per Q-OQ1 we do **not** add `package.json`-but-no-`hardhat.config` autodetection in this PR.
3. **Add fixture.** `examples/validation-fixtures/node-runtime.yml` mirroring `node-hardhat-solidity.yml` shape:
   ```
   type: node-runtime
   entry: npm
   custom_tests: "npm --workspace=apps/api run test"
   skip_tests:
     - hardhat
     - solidity
   slots:
     project_name: nightly-node-runtime
     canary_tools:
       - bash
       - node
       - npm
       - jq
     tap_plan: 3
   ```
4. **Doc updates.** `README.md` validation section — add `node-runtime` to the supported types list with the new manifest keys (`custom_tests`, `skip_tests`). `agents.md` is unaffected.
5. **AC1 acceptance test stub.** Add `scripts/dev/test_render_node_runtime.sh` (or extend the existing render test script if present) that loads `examples/validation-fixtures/node-runtime.yml`, calls `render_validation_templates.py`, and asserts: (a) `tests/30_hardhat_test.sh` is absent from rendered output, (b) `validate.env` contains `CUSTOM_TESTS_CMD=` and `SKIP_TESTS=`, (c) `Dockerfile.app` starts with `FROM node:22`. No real Docker run needed in the unit test — that comes via the bitsafe.io consumer once Fix 1 is in `@stable`.

### PR B — Fix 2: distinguish `harness_error` everywhere

6. **Surface `raw_status` from validation run.** Extend the existing per-cycle `gh run view` lookup in `scripts/orchestrate_poll_process.sh` (search the orchestrator's existing `get_last_validation_run_*` helpers near L8430–8460 and `dispatch_validation_if_needed`) to also extract `outputs.raw_status` from the dispatched run via `gh run view --json jobs,conclusion,outputs <run-id>`. Per Q3, this is the canonical signal; cache cycle-locally as `LAST_VAL_RAW_STATUS`. Per §15, the call extends an existing one rather than adding a new request.
7. **Branch on `raw_status` before budget decrement.** In `mark_validation_failed` (`L4360-4435`): when `LAST_VAL_RAW_STATUS == "harness_error"`, **do not** increment `val_recovery_count`, **do not** advance `validation_cycle`, **do not** set `validation_failure_class` to a deterministic class that would skip the recovery budget. Instead: set `ai:harness-broken` label, keep `ai:validating` cleared of `ai:validation-recovery`, emit log `HARNESS_ERROR_DETECTED reason=<short>` (new contractual prefix; document in `agents.md` "Stable log prefixes" section), post a tracking comment explaining harness vs application failure, and trigger Step 8.
8. **Re-render the harness from latest `@stable`.** Add a `re_render_harness_from_stable` helper near `dispatch_validation_if_needed`. The helper: (a) refreshes the wrapper workflow's `@stable` pin via the existing `repository_dispatch` mechanism (per §14), (b) re-dispatches `validate.yml`. Crucially, this does NOT bump `validation_cycle` — the rationale is in the function docstring. Skip if already on latest stable in this cycle (idempotency: state-file key `last_stable_rerender_at_sha`).
9. **Suppress `judge_fingerprint_repeat_count` on harness failures.** `L11668–11680`: gate the `JUDGE_FINGERPRINT_REPEAT_COUNT` increment behind `LAST_VAL_RAW_STATUS != "harness_error"` (and ensure the same guard applies in the judge-prompt builders under `prompts/`). Reset to 0 on `harness_error` so a fixed harness doesn't carry stale repeat-count penalty.
10. **`ai:harness-broken` label registration.** Add to the label list in `_ensure_label_exists` / `reconcile_managed_issue_labels` per the existing label-creation flow in `orchestrate_poll_process.sh:6898–6901`. The label is **additive** to `ai:validation-failed` (both can be set simultaneously — `ai:validation-failed` for the phase state, `ai:harness-broken` for the failure class). When the harness is repaired (raw_status flips to `pass` or non-`harness_error` failure), clear `ai:harness-broken` automatically.
11. **Audit judge prompts.** Under `prompts/`, search for any prompt that consumes `validation_failure_reason` or `MAX_VALIDATION_RECOVERY_ATTEMPTS`/`MAX_VALIDATE_CYCLES` and make the harness/application distinction visible in the prompt context so the judge does not penalize a project for harness flakes.
12. **AC2 acceptance test stub.** Add `scripts/dev/test_harness_error_budget.sh` — sets up a fake validation run with `raw_status=harness_error`, invokes the orchestrator's validation-outcome dispatch, asserts `val_recovery_count` is unchanged in the state file and `ai:harness-broken` is set without removing `ai:validating`.

### PR C — Fixes 3 + 4 + 5 + 8

13. **Fix 3 step 1 — extract eager-draft helper.** Refactor `ensure_eager_final_pr` (`L2911`) into two paths:
    - `ensure_eager_draft_pr <integration_branch> <default_branch> <project_title>` — opens a draft PR (`gh pr create --draft`) when `git rev-list --count main..<integration_branch>` is non-zero and no open PR exists. Always returns a PR number.
    - The current ready-PR creation logic stays as a separate `promote_eager_pr_to_ready <pr_number>` (`gh pr ready <pr_number>`). Existing call sites switch to call `ensure_eager_draft_pr` first, then `promote_eager_pr_to_ready` once `ai:ready-to-merge` is set.
14. **Fix 3 step 2 — call site inversion.** The current call site at `L3378` (in `heal_integration_branch_conflict`) keeps `ensure_eager_draft_pr`. Add a new top-of-poll-loop check: as soon as the orchestrator confirms the integration branch is ahead of `main`, invoke `ensure_eager_draft_pr` regardless of validation phase. Cycle-local cache the PR number in `STATE_FILE.final_merge_pr_draft`.
15. **Fix 3 step 3 — status-section sync.** Add `update_eager_pr_validation_status_section <pr_number>` that rewrites a fenced `<!-- VALIDATION_STATUS_V1 -->...<!-- /VALIDATION_STATUS_V1 -->` block in the PR body with the last validation outcome, run URL, timestamp, and a one-line "Next action" guidance ("Awaiting validation", "Validation passing — awaiting `ai:ready-to-merge`", "Harness broken — see #<issue>", etc.). Idempotent: same payload → no API call (compare body checksum first).
16. **Fix 3 step 4 — draft → ready transition.** When `ai:ready-to-merge` is applied to the tracking issue, the existing promotion logic now calls `promote_eager_pr_to_ready` instead of opening a new PR. When `ai:harness-broken` is set, the draft stays as-is and `update_eager_pr_validation_status_section` posts a comment if the section's harness-broken state is new (debounced).
17. **Fix 4 step 1 — schema definition.** Define `validation_history.v1` schema in `scripts/ai_memory.py` or in a new module-doc comment: `{schema_version, integration_branch_sha, integration_branch_name, outcome, raw_status, run_id, run_url, timestamp_utc, validation_cycle, tracking_issue}`. File path on `ai-memory` branch: `validation_history/<repo-slug>/<sha-prefix>/<sha>.json` (one file per SHA, append entries on each validation outcome).
18. **Fix 4 step 2 — write path.** Where the orchestrator finalizes a validation outcome (the same place Step 7 detects `raw_status`), call a new `memory_record_validation_history` helper that appends to the file. Reuse the existing `memory_record_candidate` plumbing — pass `--category validation_history` and a JSON record matching the schema. Per `scripts/memory_helpers.sh`, this is fail-open: a memory write failure does not block the orchestrator.
19. **Fix 4 step 3 — read path.** Before promoting `ai:validated → ai:ready-to-merge`, read `validation_history/<repo>/<sha-prefix>/<sha>.json` via a new `memory_load_validation_history_for_sha` helper (fail-open: read failure → fall back to label-only path). Promotion gate: require at least one entry with `outcome=pass` AND `raw_status=pass` AND no later entry (by `timestamp_utc`) with `outcome=fail` AND `raw_status != harness_error`. `harness_error` entries don't count for/against; they're informational.
20. **Fix 5 step 1 — staleness check.** In the per-tracking-issue poll loop, add a `check_integration_branch_staleness` helper invoked once per cycle. Inputs: integration branch name, last successful main-squash timestamp (from state file `last_main_squash_at_utc`, or fall back to the latest merged final PR's `merged_at` via the existing `gh pr list` cache). Compute hours-stale; if `>= ORCH_INTEGRATION_STALE_ALERT_HOURS` (default 6), proceed.
21. **Fix 5 step 2 — alert idempotency.** State-file key `integration_stale_last_alerted_at_utc`. If unset or `time() - integration_stale_last_alerted_at_utc >= ORCH_INTEGRATION_STALE_REALERT_HOURS*3600` (default 12), fire `tg_notify` with text "⚠️ Integration branch `<branch>` ahead of `main` for <H>h with no successful squash. Last validation: <outcome> (<run_url>) at <timestamp>. Project #<TRACKING_NUM>." Log `INTEGRATION_STALE_ALERT_SENT branch=<branch> hours=<H>` (new contractual prefix). Update `integration_stale_last_alerted_at_utc`.
22. **Fix 5 step 3 — alert clearance.** When a successful squash to `main` lands (existing path that records `last_main_squash_at_utc`), also reset `integration_stale_last_alerted_at_utc = null` so the next stall starts a fresh alerting window.
23. **Fix 8 — compose with existing `/revalidate`.** Extend the existing `/revalidate` handler at `L8669–8710`:
    - **8a:** add `--remove-label "ai:harness-broken"` to the existing label-removal sequence (no-op if absent).
    - **8b:** after the existing `set_tracking_phase_label "ai:validating"`, call `update_eager_pr_validation_status_section "${STATE_FILE.final_merge_pr_draft}"` (added in Step 15) to refresh the draft PR's status section to "Revalidating after operator reset".
    - **8c:** after the existing `tg_notify`, call `memory_record_candidate --category revalidate_events --schema-version revalidate_event.v1 --content "$(jq -n …)"` capturing `{actor: $GITHUB_ACTOR, timestamp_utc, prior_outcome, integration_sha, reason: <comment-body-tail-after-/revalidate>, tracking_issue, schema_version: "revalidate_event.v1"}`. Fail-open per `memory_helpers.sh` conventions.
    - **8d:** idempotency check — read AI-memory `revalidate_events` for the current `(tracking_issue, integration_sha)`; if an entry from the same `actor` exists within the last 5 minutes, skip dispatch and post a comment "Already processed /revalidate from @<actor> at <ts>". This dedups rapid multi-comments per the user's idempotency requirement without introducing a new state-file key.
24. **AC3 / AC4 / AC5 / AC8 acceptance test stubs.** One per fix, under `scripts/dev/`: simulate state-file inputs + cached API responses, assert that the orchestrator's poll-cycle dispatch produces the expected gh / memory / log side-effects.

### PR D — Fixes 6 + 7

25. **Fix 6 step 1 — `ai:force-merge` label registration.** Add to `_ensure_label_exists` / `reconcile_managed_issue_labels`. Document in README as the operator escape hatch.
26. **Fix 6 step 2 — bypass dispatch.** Near the existing `ai:ready-to-merge` promotion logic, add a `check_force_merge_bypass` helper. When `ai:force-merge` is present on the tracking issue AND the integration branch has commits ahead AND a draft PR exists (Fix 3): (a) call `promote_eager_pr_to_ready`, (b) post a comment on the tracking issue + PR citing the last validation run URL and the actor who applied the label, (c) write a `bypasses.v1` audit memory record `{actor, timestamp_utc, integration_sha, last_validation_run_url, last_validation_raw_status, reason: <body-of-tracking-issue-or-comment>, tracking_issue, schema_version: "bypasses.v1"}` via `memory_record_candidate --category bypasses --confidence 1.0`. Log `FORCE_MERGE_BYPASS actor=<actor> issue=<n> pr=<n>` (new contractual prefix).
27. **Fix 6 step 3 — actor sourcing.** Per §15, query `gh api repos/${GITHUB_REPOSITORY}/issues/${TRACKING_NUM}/events?per_page=20` once per cycle when `ai:force-merge` is observed (gated behind label presence — zero cost on the hot path), filter for the most recent `event=labeled` with `label.name == "ai:force-merge"`, use that `actor.login`. Fall back to `${GITHUB_ACTOR:-orchestrator-bot}` if the events API call fails.
28. **Fix 6 step 4 — idempotency.** State-file key `force_merge_bypass_applied_at_sha`. Skip the bypass if the recorded SHA matches the current integration-branch HEAD (one bypass per SHA). On new commit / new SHA, the bypass can fire again if `ai:force-merge` remains applied.
29. **Fix 7 step 1 — ahead-count check.** Before each `ai/issue-N` PR merge into the integration branch (find the existing call site near `L301` and `L619`), call a new `check_integration_backpressure` helper. Inputs: integration branch, `main` ref. Compute `git rev-list --count main..<integration_branch>` (already done elsewhere; reuse cycle cache `_integration_ahead_count`). If `>= ORCH_INTEGRATION_MAX_AHEAD_COMMITS` (default 10): refuse the merge.
30. **Fix 7 step 2 — backpressure response.** On refused merge: (a) post a comment on the issue PR explaining the deferral and linking to the open draft squash PR (Fix 3), (b) label the tracking issue `ai:integration-backpressure`, (c) log `BACKPRESSURE_TRIGGERED ahead=<count> max=<max> issue=<n>` (new contractual prefix). Do NOT close the issue PR — leave it open so it merges automatically once the backlog clears.
31. **Fix 7 step 3 — backpressure clearance.** When a successful squash to `main` lands, `git rev-list --count main..<integration_branch>` drops below the threshold; the next poll cycle removes `ai:integration-backpressure` and proceeds with pending `ai/issue-N` merges in FIFO order.
32. **AC6 / AC7 acceptance test stubs.** One per fix under `scripts/dev/`.

### Final step (all PRs)

33. **`mark-stable.sh` after each PR.** Per §14, the existing `repository_dispatch` flow propagates the `@stable` tag to every consumer in `.github/ai/consumer_repos.json` (10 repos). No per-consumer YAML edits required.

## Files & Modules

PR A (Fix 1):
- `workflow-templates/validation-harness/node-runtime/Dockerfile.app.j2` [new]
- `workflow-templates/validation-harness/node-runtime/docker-compose.test.yml.j2` [new]
- `workflow-templates/validation-harness/node-runtime/validate.env.j2` [new]
- `workflow-templates/validation-harness/node-runtime/tests/00_canary.sh.j2` [new]
- `workflow-templates/validation-harness/node-runtime/tests/10_family_marker.sh.j2` [new]
- `workflow-templates/validation-harness/node-runtime/tests/20_import_audit.sh.j2` [new]
- `workflow-templates/validation-harness/node-runtime/tests/50_custom_tests.sh.j2` [new]
- `workflow-templates/validation-harness/node-runtime/tests/90_tap_report.sh.j2` [new]
- `scripts/render_validation_templates.py` (extend `FAMILY_REGISTRY` at L95–98)
- `examples/validation-fixtures/node-runtime.yml` [new]
- `scripts/dev/test_render_node_runtime.sh` [new]
- `README.md` (extend validation type list)

PR B (Fix 2):
- `scripts/orchestrate_poll_process.sh` (extend `mark_validation_failed` L4360–4435; `_ensure_label_exists` L6898–6901; `judge_fingerprint_repeat_count` block L11668–11680; new `re_render_harness_from_stable` helper)
- `prompts/mode-judge.txt`, `prompts/mode-orchestrate-poll-judge.txt`, `prompts/mode-validate-*.txt` (audit for harness/application distinction)
- `agents.md` (add `HARNESS_ERROR_DETECTED` to "Stable log prefixes" list at L136)
- `scripts/dev/test_harness_error_budget.sh` [new]

PR C (Fixes 3 + 4 + 5 + 8):
- `scripts/orchestrate_poll_process.sh` (refactor `ensure_eager_final_pr` at L2911 into `ensure_eager_draft_pr` + `promote_eager_pr_to_ready`; new `update_eager_pr_validation_status_section`, `memory_record_validation_history`, `memory_load_validation_history_for_sha`, `check_integration_branch_staleness`, extend `/revalidate` handler at L8669–8710)
- `scripts/ai_memory.py` (document new schemas: `validation_history.v1`, `revalidate_event.v1`)
- `scripts/memory_helpers.sh` (no new functions if `memory_record_candidate` covers it; otherwise add a thin `memory_record_validation_history` wrapper)
- `agents.md` (add `INTEGRATION_STALE_ALERT_SENT`, `EAGER_DRAFT_PR_CREATED`, `EAGER_DRAFT_PR_PROMOTED` to "Stable log prefixes")
- `README.md` (env-var table: add `ORCH_INTEGRATION_STALE_ALERT_HOURS`, `ORCH_INTEGRATION_STALE_REALERT_HOURS`)
- `.github/workflows/orchestrate_poll.yml` (env-var defaults if exposed at workflow scope)
- `scripts/dev/test_eager_draft_pr.sh`, `scripts/dev/test_validation_history.sh`, `scripts/dev/test_integration_staleness.sh`, `scripts/dev/test_revalidate_compose.sh` [new]

PR D (Fixes 6 + 7):
- `scripts/orchestrate_poll_process.sh` (new `check_force_merge_bypass`, `check_integration_backpressure` helpers; extend label registration; extend pre-merge dispatch at L301 / L619)
- `agents.md` (add `FORCE_MERGE_BYPASS`, `BACKPRESSURE_TRIGGERED` to "Stable log prefixes")
- `README.md` (env-var table: add `ORCH_INTEGRATION_MAX_AHEAD_COMMITS`; document `ai:force-merge` and `ai:integration-backpressure` in operator section)
- `.github/workflows/orchestrate_poll.yml` (env-var default if exposed at workflow scope)
- `scripts/dev/test_force_merge_bypass.sh`, `scripts/dev/test_integration_backpressure.sh` [new]

No deletions. No renames. `node-hardhat-solidity` is untouched.

## Data Model / Index Changes

N/A — no MongoDB collections in this repo.

Persistent stores touched and their new shapes:

- **Orchestrator state file (`STATE_FILE`, JSON on disk).** New keys (all backward-compatible additions, default values explicit):
  - `last_main_squash_at_utc` (epoch int, default `null`) — set on each successful squash to `main`.
  - `integration_stale_last_alerted_at_utc` (epoch int, default `null`) — Fix 5 alert dedup.
  - `last_stable_rerender_at_sha` (string, default `""`) — Fix 2 idempotency for harness re-render.
  - `final_merge_pr_draft` (int, default `null`) — Fix 3 draft PR number cycle cache.
  - `force_merge_bypass_applied_at_sha` (string, default `""`) — Fix 6 per-SHA bypass dedup.

- **AI-memory branch (`ai-memory`).** New file shapes (each carries explicit `schema_version`):
  - `validation_history/<repo>/<sha-prefix>/<sha>.json` — `validation_history.v1` records.
  - Memory candidates with `category=bypasses` — `bypasses.v1`.
  - Memory candidates with `category=revalidate_events` — `revalidate_event.v1`.

All AI-memory writes are fail-open per `scripts/memory_helpers.sh` conventions; failures degrade to telemetry warnings and don't break the orchestrator loop.

## Tests

Unit / dev tests (under `scripts/dev/`, one per AC):

- `test_render_node_runtime.sh` (AC1) — render fixture, assert generated file set + env-var content.
- `test_harness_error_budget.sh` (AC2) — feed `raw_status=harness_error`, assert no budget decrement.
- `test_eager_draft_pr.sh` (AC3) — feed "branch ahead, no PR", assert `gh pr create --draft` is called with the right args.
- `test_validation_history.sh` (AC4) — record pass + harness_error + pass for same SHA, assert promotion gate passes.
- `test_integration_staleness.sh` (AC5) — feed `last_main_squash_at_utc = now-7h`, assert one alert; feed `now-13h` after alert, assert re-alert; feed `now-7h` immediately after, assert no alert.
- `test_force_merge_bypass.sh` (AC6) — apply label, assert promotion + audit comment + memory record + no rename of existing labels.
- `test_integration_backpressure.sh` (AC7) — feed 10 commits ahead, assert merge refusal + label + comment.
- `test_revalidate_compose.sh` (AC8) — feed `ai:validation-failed + ai:harness-broken`, assert both cleared + status section refreshed + memory record written.

End-to-end verification: the bitsafe.io consumer becomes the real-world test bed for AC1 once PR A is in `@stable`. ACs 2–8 require shell-level unit tests because end-to-end would need a live orchestrator instance and real GitHub state; the unit-test stubs above mock the gh/memory surface and assert on the orchestrator's intended side-effects.

CI integration: `scripts/dev/run_all_tests.sh` (extend if it exists; otherwise the new test scripts are runnable individually). No new GitHub Actions workflow added — these are local pre-push tests.

## Risks & Mitigations

- **Risk:** Fix 3 changes the lifecycle in a way that breaks the existing `ensure_eager_final_pr` call site in `heal_integration_branch_conflict` (`L3378`). **Mitigation:** the refactor preserves the existing function's behavior under a new name (`ensure_eager_draft_pr` + `promote_eager_pr_to_ready`); the call site at L3378 keeps working because the conflict-resolver path always wants a PR (draft or ready). Verify by re-running the existing integration-conflict tests if any.
- **Risk:** Fix 4 promotion-gate change ("≥1 pass + no fail since latest commit") could regress existing repos where validation has historically passed once and never been re-checked. **Mitigation:** fail-open — if AI-memory read fails or returns empty, fall back to the current label-only promotion path. Document the change in `README.md` so operators understand the new gate.
- **Risk:** Fix 2 harness re-render could loop forever if `@stable` itself is broken. **Mitigation:** the `last_stable_rerender_at_sha` state-file key caps re-renders to one per `@stable` SHA per cycle; deterministic re-failures escalate to `ai:harness-broken` (visible label) and the `/revalidate` reset path remains available for operator intervention.
- **Risk:** Fix 5 Telegram alerts could spam if `last_main_squash_at_utc` is never set (e.g. on first-ever run). **Mitigation:** treat missing `last_main_squash_at_utc` as "no stale window yet" (suppress alert on bootstrap); only start the staleness clock once an integration branch has been created.
- **Risk:** Fix 6 `ai:force-merge` could be misused (operator bypasses real defects). **Mitigation:** the `bypasses.v1` audit record on the AI-memory branch is indefinite; PR comment includes the actor and the failing validation run URL; behavior is documented prominently in README under "Operator escape hatches".
- **Risk:** Fix 7 backpressure could deadlock if the squash PR itself is blocked (e.g. conflict). **Mitigation:** the conflict-resolver path (`heal_integration_branch_conflict`) already exists and dispatches autofix on the squash PR; Fix 7 does not change that. If autofix can't resolve, Fix 5 will alert and Fix 6 (`ai:force-merge`) provides the operator escape hatch.
- **Risk:** §6 violation if any rename is introduced. **Mitigation:** every new identifier is added alongside; the test stubs assert no existing label / env var / log key has been removed.
- **Risk:** §15 violation if Fix 6's events-API lookup happens on every cycle. **Mitigation:** the call is gated behind `ai:force-merge` label presence — zero cost when the label is absent (the common case). Document this in the function docstring per §15's batching-contract requirement.
- **Risk:** PR C is large (4 fixes, ~10 new helpers). **Mitigation:** commit hygiene per CLAUDE.md §12.E — one logical commit per Fix 3/4/5/8 step group; reviewers can scope to individual commits.
- **Risk:** Consumer-side propagation lag. **Mitigation:** `mark-stable.sh` after each PR + the existing `repository_dispatch` to all 10 consumers in `.github/ai/consumer_repos.json` keeps the lag bounded; bitsafe.io is the canary.

## Rollout

Per-PR rollout:

- **PR A (Fix 1):** purely additive — new family, new fixture. Merge → `mark-stable.sh` → consumers pick up. bitsafe.io can immediately set `type: node-runtime` in `.ai/validate.yml` to validate AC1.
- **PR B (Fix 2):** changes orchestrator dispatch behavior. Backward-compatible: the existing `ai:validation-failed` flow still fires; `ai:harness-broken` is purely additive on top. No state-file schema migration needed (`last_stable_rerender_at_sha` defaults to empty string if absent).
- **PR C (Fixes 3 + 4 + 5 + 8):** introduces draft PRs and the per-SHA history gate. Risk: existing in-flight orchestrator projects might be mid-cycle when PR C lands. Mitigation: the new state-file keys default sensibly; `final_merge_pr_draft = null` means the orchestrator falls through to the existing (ready) PR path on first encounter. Per-SHA history starts empty and the read path fails open. **No migration script needed.**
- **PR D (Fixes 6 + 7):** label-driven; opt-in behavior (`ai:force-merge` is operator-applied). Backpressure (Fix 7) is automatic once `ORCH_INTEGRATION_MAX_AHEAD_COMMITS=10` is set; the default is conservative.

No feature flag introduced. The minimal-change set principle (§5) plus the additive label / env-var convention provides the rollback safety: each new behavior can be disabled per-consumer by overriding the relevant env var to a sentinel (e.g. `ORCH_INTEGRATION_MAX_AHEAD_COMMITS=999` effectively disables Fix 7). Document these overrides in `README.md`.

Consumer propagation:

- After each PR merges, run `scripts/mark-stable.sh`. The existing `repository_dispatch` mechanism (per `agents.md` §19) notifies all 10 consumers in `.github/ai/consumer_repos.json`. Their wrapper workflows (`ai-*.yml`) pin `@stable` and pick up changes on next dispatch. No per-consumer YAML edits.

Telemetry:

- New `HARNESS_ERROR_DETECTED`, `INTEGRATION_STALE_ALERT_SENT`, `EAGER_DRAFT_PR_*`, `FORCE_MERGE_BYPASS`, `BACKPRESSURE_TRIGGERED` log prefixes are added to `agents.md` "Stable log prefixes" list so workflow-log-analysis picks them up.

## Open Questions

These are non-blocking — defaults are chosen in the plan and called out here for reviewer override.

- **Q-OQ1 (Fix 1 fallback policy):** should `package.json`-but-no-`hardhat.config.*` and no `foundry.toml` auto-select `node-runtime`? **Plan default: NO (explicit `manifest.type` only).** Auto-selection adds a heuristic with surprising behavior on monorepos. Override in review if a different policy is preferred.
- **Q-OQ2 (Fix 5/7 env-var defaults):** `ORCH_INTEGRATION_STALE_ALERT_HOURS=6`, `ORCH_INTEGRATION_STALE_REALERT_HOURS=12`, `ORCH_INTEGRATION_MAX_AHEAD_COMMITS=10` — match the source proposal verbatim. **Plan default: as proposed.** Override in review if operations wants different thresholds.
- **Q-OQ3 (schema version strings):** `validation_history.v1`, `bypasses.v1`, `revalidate_event.v1` — these mirror the existing `run_ledger_entry.v1` / `workflow_log_analysis_cache.v1.json` convention. **Plan default: as written.** Override if the AI-memory team uses a different naming.
- **Q-OQ4 (Fix 1 Node major):** `FROM node:22-bookworm` — Node 22 is the current LTS as of 2026-05-17; the source proposal said "Node 22+". **Plan default: Node 22.** Override to 24-current or `lts/*` if preferred.
- **Q-OQ5 (Fix 4 promotion-gate strictness):** the proposed gate is "≥1 pass AND no fail since the most recent integration-branch commit". An alternative is "exactly the latest outcome is pass" — stricter, less history-aware. **Plan default: the proposed cumulative gate.** Override if the strict-latest rule is preferred.
- **Q-OQ6 (Fix 6 events-API call cost):** the `gh api repos/.../issues/{n}/events` lookup is gated behind `ai:force-merge` label presence (zero cost when absent). If `ai:force-merge` is applied to many issues simultaneously, this becomes one call per issue per cycle. If that's a concern, the alternative is to extend the existing per-cycle `_fetch_candidate_issue_details_graphql` to also return label-event actors. **Plan default: gated-events-API call.** Override if §15 hygiene wants the GraphQL extension instead.
- **Q-OQ7 (interactive-Claude vs unattended-pipeline path):** every fix here will run inside `orchestrate_poll.yml`, which is governed by `unattended_system_instructions.md` (bias-to-action, no STOP-and-ASK). This plan is being written under `CLAUDE.md` rules (interactive). When implementing, be explicit in commit messages and code comments about which ruleset applies. **No override needed; reviewer awareness only.**

## References

- Source proposal: full text in conversation history (issue raised against `shubhodeep1/coding-workflows` from consumer `shubhodeep1/bitsafe.io`).
- Existing `/revalidate` implementation: `scripts/orchestrate_poll_process.sh:8669–8710`.
- Existing `/revalidate` docs: `README.md:1405–1414`.
- `ensure_eager_final_pr`: `scripts/orchestrate_poll_process.sh:2911`.
- `mark_validation_failed`: `scripts/orchestrate_poll_process.sh:4360–4435`.
- `validate.yml` exit-2 / `raw_status`: `.github/workflows/validate.yml:33–35, 805, 824–838, 1056, 1062–1067`.
- `FAMILY_REGISTRY`: `scripts/render_validation_templates.py:95–98`.
- `python-repo-checks` family (the shape `node-runtime` mirrors): `workflow-templates/validation-harness/python-repo-checks/`.
- AI-memory primitives: `scripts/memory_helpers.sh:100–128` (`memory_record_run_event`, `memory_record_candidate`).
- Consumer registry: `.github/ai/consumer_repos.json` (10 repos; bitsafe.io is listed).
- `tg_notify` (Telegram alert path): `scripts/orchestrate_poll_process.sh:68–96`.
- Stable log prefixes contract: `agents.md:130–147`.
- Project rules: `CLAUDE.md` §4, §6, §9, §10, §14, §15; `unattended_system_instructions.md` §8, §10, §11, §14.
