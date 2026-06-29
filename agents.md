# agents.md — Repo Architecture Facts (coding-workflows)

This file contains **repo-specific architectural facts** for any AI agent
(interactive Claude session, codex-cli unattended pipeline, third-party
reviewer model). Global engineering rules live in `CLAUDE.md` (interactive)
or `unattended_system_instructions.md` (unattended) — do not duplicate them
here.

Consumer repos define their own `agents.md` with their own architectural
facts. The unattended pipeline loads this file as `agents_canonical.md` and
the consumer's `agents.md` separately; both are inlined into the prompt.

---

## Workflow architecture

Phases of the unattended pipeline (each is a separate workflow file under
`.github/workflows/`):

1. **clarify** (`clarify.yml`, `internal-clarify.yml`) — read the issue,
   decide whether clarifying questions are needed, emit `STATUS: CLEAR` or
   a `Q1`/`Q2` batch.
2. **clarify-respond** (`orchestrate_clarify_respond.yml`) — answer the
   clarifier's questions on behalf of an orchestrator-managed issue.
3. **plan** (`plan.yml`, `internal-plan.yml`) — read the clarified issue and
   emit a structured implementation plan with files-to-change and a
   per-issue ≤60-minute time budget.
4. **implement** (`implement.yml`, `internal-implement.yml`) — execute the
   plan with codex-cli; write the actual files.
5. **implement-diagnose** (`scripts/implement_diagnose_post_codex_failure.sh`,
   driven by `MODEL_DIAGNOSE`) — analyse a post-Codex validation failure and
   emit JSON fix-up issue proposals.
6. **implement-repair** (`prompts/mode-implement-repair.txt`,
   `mode-implement-repair-syntax.txt`) — narrow post-Codex repair runs.
7. **review autofix** (`review_autofix.yml`, `internal-review.yml`) — multi-
   model reviewer + consolidator + editor loop on PR changes.
8. **conflict resolver** (`prompts/conflict-resolver.txt`,
   `integration-sync-conflict-resolver.txt`) — merge-conflict resolution
   inside autofix.
9. **orchestrate** (`orchestrate.yml`, `orchestrate_poll.yml`) — issue
   decomposition + judge polling.
10. **judge** (`mode-judge.txt`, `mode-orchestrate-poll-judge.txt`,
    `mode-judge-review-blocked.txt`, `mode-judge-stall-recovery.txt`) —
    JSON-emitting evaluation of wave state.
11. **validate** (`validate.yml`, `mode-validate-*.txt`) — generate / fix /
    self-heal a validation harness for the implemented change.
12. **workflow log analysis** (`workflow-log-analysis.yml`,
    `mode-workflow-*.txt`) — periodic audit of workflow runs.
13. **check failure triage** (`check_failure_triage.yml`,
    `internal-check-failure-triage.yml`, `scripts/check_failure_triage.sh`,
    `prompts/mode-check-failure-triage.txt`) — triggers on `check_run:
    completed` failures on a PR; the diagnosis model analyses the failing
    check's logs and opens a GitHub issue (label `ai:check-triage`) describing
    the root cause + suggested fix, which the clarify→…→review pipeline then
    picks up. On by default; disable per repo via
    `CHECK_FAILURE_TRIAGE_ENABLED=false`; never pushes code itself. De-dupes one
    in-flight triage per repo+PR+check and caps the
    auto-fix lineage at `CHECK_FAILURE_TRIAGE_MAX_LINEAGE_DEPTH` generations
    (escalates with `ai:check-triage-escalated` + Telegram at the cap).

---

## Models in use (defaults; overridable via repo-vars)

| Phase | Default model | Default reasoning | Verbosity |
|---|---|---|---|
| clarify, clarify-respond | `openai/gpt-5.4` | `xhigh` (smoke: `low` — `clarify.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` |
| plan | `openai/gpt-5.4` | `xhigh` (smoke: `low` — `plan.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` |
| orchestrate (decompose), judge | `openai/gpt-5.4` | `xhigh` | `low` |
| implement (main editor) | `openai/gpt-5.4` | `xhigh` (smoke: no override — see `.github/workflows/implement.yml:597-606`) | `low` |
| implement-repair, implement-repair-syntax | `openai/gpt-5.4` | `xhigh` | `low` |
| implement-diagnose | `openai/gpt-5.4` | `xhigh` | `low` |
| review autofix editor | `openai/gpt-5.4` | `xhigh` (smoke: `medium`) | `low` |
| review autofix reviewers (pass 1) | `REVIEWER_MODELS` (default roster: `minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, `deepseek/deepseek-v4-pro`, `mistralai/mistral-small-2603`, `qwen/qwen3.6-plus`, `x-ai/grok-4.20`) | `xhigh` per reviewer call (hardcoded at the `run_reviewer_pass ... "xhigh"` callsite in `scripts/review_run_reviewers.sh:1709`; not affected by the smoke `REVIEWER_REASONING_EFFORT=low` override in two-pass mode) | `low` |
| review autofix reviewers (pass 2) | `REVIEWER_MODELS` (same roster, after pass-2 scope / tier filtering) | `high` on diffs below `REVIEWER_PASS2_DIFF_LARGE_LOC=200`, `xhigh` at or above that threshold; smoke: `low`; operator override wins | `low` |
| review consolidator | `openai/gpt-5.4` | `xhigh` | `low` |
| conflict resolver | `openai/gpt-5.4` | `high` (decoupled from smoke; `scripts/review_conflict_resolve.sh` validates `xhigh`, `high`, `medium`, `none` only — `low` is rejected; default lowered from `xhigh` after runs `25627236793` / `25627316961` hit `timeout`-killed retries on degenerate orchestrator-stack integrations; override per-repo via `vars.THINKING_LEVEL_CONFLICT_RESOLVER`) | `low` |
| validate generate, diagnose | `openai/gpt-5.4` | `xhigh` | `low` |
| validate discover | `openai/gpt-5.4` | `xhigh` (per-phase override via `MODEL_REASONING_EFFORT_DISCOVER`) | `low` |
| validate fix-harness, self-heal | `openai/gpt-5.4` | `xhigh` | `low` |
| workflow log analyze | `openai/gpt-5.4` | `xhigh` | `low` |
| workflow audit | `openai/gpt-5.4` | `xhigh` (hardcoded in `.github/workflows/workflow-log-analysis.yml:716-717`) | `low` |
| workflow api-redundancy | `openai/gpt-5.4` | `xhigh` (default of `THINKING_LEVEL_ANALYSIS`) | `low` |
| workflow log summary | `openai/gpt-5.4-mini` | default | `low` |
| reviewer consensus summariser | `openai/gpt-5.4-mini` | `medium` (`XPOLL_SUMMARISER_REASONING`) | `low` |

All gpt-5.4 phases now resolve to `low` verbosity at every layer: the per-phase
`MODEL_VERBOSITY` env-var default in `.github/workflows/*.yml` (`VERBOSITY_*`
repo-vars), the `-c model_verbosity=low` CLI flag on every `codex exec`
callsite (≈20 sites across `scripts/*.sh` and `.github/workflows/*.yml`),
the `model_verbosity = "low"` line that `scripts/write_codex_config.sh:242`
writes into `config.toml`, and the `"default_verbosity": "low"` for
`openai/gpt-5.4` in `scripts/codex_model_catalog.json:354`. Third-party
reviewer models (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`,
`deepseek/deepseek-v4-pro`, `mistralai/mistral-small-2603`,
`qwen/qwen3.6-plus`, `x-ai/grok-4.20`)
carry `support_verbosity = false` in the catalog — codex CLI logs
`model_verbosity is set but ignored as the model does not support verbosity`
and continues; the value is operationally moot for those rows. The
historical `high` value across every layer was a workaround for the
openai/codex#11151 announce-without-emit failure mode (implement /
review_autofix smoke runs at 2026-05-07 12:41 / 12:42, where the model
emitted a reasoning trace and exited without a tool call); the workaround
now relies on `include_apply_patch_tool = true` as the primary
belt-and-suspenders. If the announce-without-emit pattern recurs at `low`,
raise verbosity at the layer that needs it (start with the editor /
implement callsites, since those are the original 11151 reproducers).

Every editor / consolidator / resolver phase now defaults to `openai/gpt-5.4`.
Reviewer fan-out remains driven by the `REVIEWER_MODELS` roster in
`.github/workflows/review_autofix.yml` (currently the third-party models
listed in the table above). The previous legacy editor split (patch-heavy
phases on a separate older slug) was retired after the announce-without-emit
regression (openai/codex#11151) drove repeat no-edit failures. The
2026-05-07 ablation suite then identified the underlying root cause as
`apply_patch_tool_type: "freeform"` on the OpenRouter Responses path (see
the `openai/gpt-5.4` catalog entry — `apply_patch_tool_type` is now
`function`).

The reviewer-only multi-model run (claude-branch-review) uses third-party
models (`minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`,
`deepseek/deepseek-v4-pro`, `mistralai/mistral-small-2603`,
`qwen/qwen3.6-plus`, `x-ai/grok-4.20`) plus
`unattended_system_instructions.md` as system context.

---

## Repo-specific batching helpers

The following helpers are the canonical batched GraphQL paths for the
GitHub API hygiene rules in `unattended_system_instructions.md` §14:

- `_fetch_candidate_issue_details_graphql` (in `scripts/orchestrate_poll_process.sh`)
- `_fetch_linked_pr_status_graphql` (in `scripts/orchestrate_poll_process.sh`)

BATCH_HELPER.name=_fetch_candidate_issue_details_graphql kind=graphql-batch path=scripts/orchestrate_poll_process.sh cache=_candidate_details_json
BATCH_HELPER.name=_fetch_linked_pr_status_graphql kind=graphql-batch path=scripts/orchestrate_poll_process.sh cache=STALL_MANAGED_LINKED_PR_CACHE

Both return a dict keyed by issue number so the caller can drop the result
into a cycle-local cache.

Cycle-local caches that must not be re-fetched per iteration:
`ACTIVE_WORKFLOW_ISSUES`, `STALL_MANAGED_LINKED_PR_CACHE`,
`_candidate_details_json`.

## Workflow install profiles

PROFILE.default=full
PROFILE.name=core manifest=workflow-templates/profiles/core.txt wrappers=ai-clarify.yml,ai-plan.yml,ai-implement.yml,ai-review.yml,ai-issue-pr-status.yml,ai-cancel-on-pr-close.yml
PROFILE.name=standard manifest=workflow-templates/profiles/standard.txt wrappers=ai-clarify.yml,ai-plan.yml,ai-implement.yml,ai-review.yml,ai-issue-pr-status.yml,ai-cancel-on-pr-close.yml,ai-orchestrate.yml,ai-orchestrate-poll.yml,ai-orchestrate-clarify-respond.yml,ai-validate.yml,ai-sync-labels.yml,review_rb_judge_dispatch.yml
PROFILE.name=full manifest=workflow-templates/profiles/full.txt wrappers=ai-cancel-on-pr-close.yml,ai-check-failure-triage.yml,ai-clarify.yml,ai-implement.yml,ai-issue-pr-status.yml,ai-memory-maintenance.yml,ai-orchestrate-clarify-respond.yml,ai-orchestrate-poll.yml,ai-orchestrate.yml,ai-plan.yml,ai-review.yml,ai-sync-labels.yml,ai-update-workflows.yml,ai-validate.yml,review_rb_judge_dispatch.yml

---

## Optional `.github/ai` operator surfaces

The Symphony closeout left three consumer-authored config surfaces on current
HEAD. Each fails open when its file is missing; a consumer enables one by
committing the corresponding file:

- `.github/ai/WORKFLOW.md` — loaded by `scripts/load_workflow_overlay.py`
  and validated by `ai-memory/schemas/workflow_overlay.v1.json`. The shipped
  schema is intentionally narrow: `schema_version` plus `prompt_overrides[]`
  append/replace entries only. This repository ships a no-op overlay
  (`schema_version` only, no `prompt_overrides`), so `WORKFLOW_OVERLAY_ENABLED`
  is `true` but no rendered prompt is altered until override entries are added.
- `.github/ai/concurrency_caps.yml` — parsed by
  `scripts/orchestrate_lib.py::load_concurrency_caps`. Missing or empty files
  disable the cap layer and restore legacy uncapped dispatch.
- `.github/ai/workspace_hooks/<phase>/<hook>.sh` — executed by
  `scripts/run_workspace_hook.sh`. Supported hook names are `after_create`,
  `before_run`, `after_run`, and `before_remove`; missing files are a no-op.

---

## Run-substate ledger + state-snapshot contract

- `scripts/ledger_emit_substate.sh` writes additive run-attempt telemetry into
  `ai-memory:runs/<run-id>/ledger/events.jsonl`, and the open metadata bag in
  `ai-memory/schemas/run_ledger_entry.v1.json` is the authoritative schema
  contract for the shipped `run_substate` + token fields.
- Common shipped `run_substate` values are `PreparingWorkspace`,
  `BuildingPrompt`, `LaunchingAgentProcess`, `InitializingSession`,
  `StreamingTurn`, `Finishing`, `Succeeded`, `Failed`, `TimedOut`, and
  `Stalled`.
- Stall-sidecar markers are emitted as separate ledger event types
  `codex_stall_observed` and `codex_stall_killed` rather than as
  `run_substate` values.
- `scripts/build_state_snapshot.py` builds the poller's `state.json` artifact,
  and `ai-memory/schemas/state_snapshot.v1.json` is the authoritative schema
  for that payload.

---

## Orchestrator tracking-issue comment markers

The orchestrator poller (`scripts/orchestrate_poll_process.sh`) maintains
two distinct marker-keyed comment families on each tracking issue. Both edit
in place every poll cycle so the tracking issue stays a live status
dashboard without producing a fresh comment per tick.

| Marker | Helper | Purpose |
|---|---|---|
| `<!-- ORCHESTRATOR_STATE_V2 part=N/N manifest=<sha> -->` … `<!-- ORCHESTRATOR_STATE_V2 -->` | `post_state_comment` / `_post_state_comment_v2_chunk` | Canonical machine-readable orchestrator state snapshot. Multi-chunk so it can carry state blobs >65 KiB. Reader: `extract_latest_valid_orchestrator_state`. Reader falls back to the legacy V1 marker `<!-- ORCHESTRATOR_STATE_V1 -->` for issues that have not yet been re-written. |
| `<!-- orchestrator:completion-status -->` | `update_completion_status_comment` | Human-readable "what is blocking completion" summary. Second-line tag `<!-- status:<token> -->` exposes the canonical status token (`in-progress` \| `waiting` \| `ready` \| `validated` \| `failed`) for grep-friendly downstream parsing. Idempotent — skips the API call when the rendered body already matches, and persists `.completion_status_comment_id` + `.completion_status_comment_body_hash` in the state file so edit-in-place fallback survives the next cron invocation. |

The completion-status comment is updated from three call sites:

1. The cycle-level wave-status decision block (every poll tick — derives
   `in-progress` / `waiting` / `ready` / `failed` from the
   `check-wave-status` JSON plus the live validation-recovery state,
   and fires a once-per-project `tg_notify` CRITICAL on the first
   `any_failed=true` observation, guarded by
   `.completion_status_failure_alert_sent` in the state file; compare-API
   failures surface as an explicit "integration status is unknown"
   line instead of silently omitting the integration gate state).
2. `mark_validation_complete` — final transition to `validated`.
3. `mark_validation_failed` — transitions to `in-progress` during
   validation recovery, and to `failed` on both terminal branches (the
   deterministic-class short-circuit and the recovery-budget-exhausted
   path).

The same change also adds a defensive preflight inside
`dispatch_validation_if_needed`: when the current wave's PRs are not all
merged into the integration branch (`WAVE_COMPLETE != "true"`) at dispatch
time (rare race against label-reconciliation, or a wave PR that transitioned
back from merged), the dispatch is skipped this cycle so the
runtime-validation workflow is not burned on a state that cannot pass. When
the validating / `/revalidate` paths reach the helper before the loop's main
wave-status block has populated the scratch `WAVE_COMPLETE` / `ANY_FAILED`
variables, the helper recomputes live wave status first and fails closed if
the probe itself cannot run. Re-entry on the next 5-minute poll tick
converges the project once wave PRs settle.

The preflight deliberately does **not** blanket-gate on `ANY_FAILED`.
`ANY_FAILED` is broad: a wave issue legitimately closed without a merged PR
(reconciled status `"closed"` — e.g. a judge-fix-up whose premise turned out
false, so no code change was needed) yields `WAVE_COMPLETE=true` **and**
`ANY_FAILED=true` simultaneously, because `"closed"` is in the
merged/closed/skipped set that keeps `all_merged` true. Gating on
`ANY_FAILED` there deferred dispatch on every poll cycle and wedged the
project in `ai:validating` forever (validation never dispatched → never
earned `ai:validated` → integration never merged → tracking issue never
closed; real-world repro: `hylifegroup.com#3`, stuck 10 days).

But `ANY_FAILED` also covers explicit failed terminal phases (for example
`ai:plan-failed`) and the dedicated `ai:implementation-failed` status, and
those **must** continue to block validation even if the wave still reconciles
to `WAVE_COMPLETE=true`. So `check-wave-status` now emits a narrower
`validation_dispatch_safe_despite_failures` signal: it is `true` only when
every failed issue is an adjudicated closed-without-merge case (live issue
closed or `ai:closed`, with no blocking terminal-failure phase). The validate
dispatch preflight keys on `WAVE_COMPLETE` plus that finer signal, while
still logging `ANY_FAILED` for observability.

The preflight gates on the wave-merge signal (`WAVE_COMPLETE`) rather than on
`PROJECT_COMPLETE`. `PROJECT_COMPLETE`
additionally folds in `integration_contained_in_default` (`ahead_by == 0`),
but runtime validation dispatches against `ref=integration_branch`, so the
integration→default merge is **not** a precondition for validation — that
merge is performed afterward by
`mark_validation_complete → finalize_integration_merge_if_needed` once the
run earns the `ai:validated` label. Gating dispatch on `PROJECT_COMPLETE`
deadlocked any project using a separate integration branch: `ahead_by`
stays `> 0` until the final merge lands, but that merge waits for
`ai:validated`, and `ai:validated` waits for a validation run that the gate
would never dispatch (validation needs the merge; the merge needs
validation). Default-branch-only projects never hit it because
`ahead_by ≡ 0`. This is the validation-dispatch sibling of the judge
hard-guard fix for `bitsafe.io#325`, which removed the same over-broad
`ahead_by == 0` gate from the `JUDGE_STATUS=complete` override. The
judge-side override at the `JUDGE_STATUS=complete` branch remains the
primary completion gate and likewise no longer blocks on integration drift.

---

## Stable log prefixes (contractual)

Workflow-log-analysis and API-hygiene reporting depend on these stable log
prefixes. Renames are breaking unless an alongside-old shim is documented
and shipped:

- `LABEL_REPAIR`
- `LABEL_REPAIR_DIFF`
- `LABEL_SYNC_CREATED`
- `LABEL_SYNC_UPDATED`
- `LABEL_SYNC_UNCHANGED`
- `LABEL_SYNC_ERROR`
- `AUTOFIX_PEER_CHECK`
- `AUTOFIX_DISPATCH_SKIPPED`
- `AUTOFIX_DISPATCH_ISSUED`
- `AI_PHASE_FAILURE_V1`
- `AI_PHASE_GATE_V1`
- `JUDGE_INTERIM_PASS_OK`
- `JUDGE_INTERIM_PASS_FAIL`
- `JUDGE_INTERIM_PRIORS_MERGED`
- `BEHAVIOURAL_SMOKE_SYNTHESISED`
- `BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL`
- `BEHAVIOURAL_SMOKE_PRESENT_FAILED`
- `BEHAVIOURAL_SMOKE_PRESENT_PASSED`
- `REISSUE_BASELINE_PRESERVED`
- `REISSUE_BASELINE_DISCARDED`
- `REISSUE_MODE`
- `FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1`
- `FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1`
- `FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1`
- `FINGERPRINT_STATE_SELFHEAL_V1`
- `FINAL_MERGE_INELIGIBILITY_ALERT_SENT`
- `EAGER_DRAFT_PR_CREATED`
- `EAGER_DRAFT_PR_PROMOTED`
- `INTEGRATION_STALE_ALERT_SENT`
- `HARNESS_ERROR_DETECTED`
- `FORCE_MERGE_BYPASS`
- `BACKPRESSURE_TRIGGERED`
- `BACKPRESSURE_CLEARED`
- `VALIDATION_DISCOVERY_STARTED`
- `VALIDATION_DISCOVERY_AGREE`
- `VALIDATION_DISCOVERY_DISAGREE`
- `VALIDATION_DISCOVERY_PR_OPENED`
- `VALIDATION_DISCOVERY_PR_REUSED`
- `VALIDATION_DISCOVERY_FAILED`
- `VALIDATION_DISCOVERY_SKIPPED_DEDUP`
- `VALIDATION_DISCOVERY_SKIPPED_DISABLED`
- `VALIDATION_DISCOVERY_SKIPPED_BUDGET`
- `VALIDATION_DISCOVERY_DRY_RUN`
- `REVIEWER_RISK_TIER`
- `REVIEWER_FILTER_SKIP`
- `REVIEWER_FAILBACK`
- `REVIEWER_FAILBACK_UNMAPPED`
- `REVIEWER_HEALTH`
- `RE_REVIEW_SKIP`
- `CONTEXT_BUDGET_WARN`
- `CODEX_HEARTBEAT`
- `BREAK_GLASS`
- `WRITE_GUARD_BLOCK`
- `WRITE_GUARD_CONFIG_ERROR`
- `WRITE_GUARD_BYPASS_ENV`
- `DRIFT_SCAN_START`
- `DRIFT_SCAN_DIFF`
- `DRIFT_SCAN_OK`
- `DRIFT_SCAN_ERROR`

- `SEMBLE_QUERY`
- `SEMBLE_FALLBACK`
- `SERENA_QUERY`
- `SERENA_FALLBACK`
- `SERENA_PROBE`
- `drift-audit:`
- `CHECK_TRIAGE`

LOG_PREFIX.name=LABEL_REPAIR
LOG_PREFIX.name=LABEL_REPAIR_DIFF
LOG_PREFIX.name=LABEL_SYNC_CREATED
LOG_PREFIX.name=LABEL_SYNC_UPDATED
LOG_PREFIX.name=LABEL_SYNC_UNCHANGED
LOG_PREFIX.name=LABEL_SYNC_ERROR
LOG_PREFIX.name=AUTOFIX_PEER_CHECK
LOG_PREFIX.name=AUTOFIX_DISPATCH_SKIPPED
LOG_PREFIX.name=AUTOFIX_DISPATCH_ISSUED
LOG_PREFIX.name=AI_PHASE_FAILURE_V1
LOG_PREFIX.name=AI_PHASE_GATE_V1
LOG_PREFIX.name=JUDGE_INTERIM_PASS_OK
LOG_PREFIX.name=JUDGE_INTERIM_PASS_FAIL
LOG_PREFIX.name=JUDGE_INTERIM_PRIORS_MERGED
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_SYNTHESISED
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_SYNTHESIS_FAIL
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_PRESENT_FAILED
LOG_PREFIX.name=BEHAVIOURAL_SMOKE_PRESENT_PASSED
LOG_PREFIX.name=REISSUE_BASELINE_PRESERVED
LOG_PREFIX.name=REISSUE_BASELINE_DISCARDED
LOG_PREFIX.name=REISSUE_MODE
LOG_PREFIX.name=FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_STATE_SELFHEAL_V1
LOG_PREFIX.name=FINAL_MERGE_INELIGIBILITY_ALERT_SENT
LOG_PREFIX.name=EAGER_DRAFT_PR_CREATED
LOG_PREFIX.name=EAGER_DRAFT_PR_PROMOTED
LOG_PREFIX.name=INTEGRATION_STALE_ALERT_SENT
LOG_PREFIX.name=HARNESS_ERROR_DETECTED
LOG_PREFIX.name=FORCE_MERGE_BYPASS
LOG_PREFIX.name=BACKPRESSURE_TRIGGERED
LOG_PREFIX.name=BACKPRESSURE_CLEARED
LOG_PREFIX.name=VALIDATION_DISCOVERY_STARTED
LOG_PREFIX.name=VALIDATION_DISCOVERY_AGREE
LOG_PREFIX.name=VALIDATION_DISCOVERY_DISAGREE
LOG_PREFIX.name=VALIDATION_DISCOVERY_PR_OPENED
LOG_PREFIX.name=VALIDATION_DISCOVERY_PR_REUSED
LOG_PREFIX.name=VALIDATION_DISCOVERY_FAILED
LOG_PREFIX.name=VALIDATION_DISCOVERY_SKIPPED_DEDUP
LOG_PREFIX.name=VALIDATION_DISCOVERY_SKIPPED_DISABLED
LOG_PREFIX.name=VALIDATION_DISCOVERY_SKIPPED_BUDGET
LOG_PREFIX.name=VALIDATION_DISCOVERY_DRY_RUN
LOG_PREFIX.name=REVIEWER_RISK_TIER
LOG_PREFIX.name=REVIEWER_FILTER_SKIP
LOG_PREFIX.name=REVIEWER_FAILBACK
LOG_PREFIX.name=REVIEWER_FAILBACK_UNMAPPED
LOG_PREFIX.name=REVIEWER_HEALTH
LOG_PREFIX.name=RE_REVIEW_SKIP
LOG_PREFIX.name=CONTEXT_BUDGET_WARN
LOG_PREFIX.name=CODEX_HEARTBEAT
LOG_PREFIX.name=BREAK_GLASS
LOG_PREFIX.name=WRITE_GUARD_BLOCK
LOG_PREFIX.name=WRITE_GUARD_CONFIG_ERROR
LOG_PREFIX.name=WRITE_GUARD_BYPASS_ENV
LOG_PREFIX.name=DRIFT_SCAN_START
LOG_PREFIX.name=DRIFT_SCAN_DIFF
LOG_PREFIX.name=DRIFT_SCAN_OK
LOG_PREFIX.name=DRIFT_SCAN_ERROR
LOG_PREFIX.name=SEMBLE_QUERY
LOG_PREFIX.name=SEMBLE_FALLBACK
LOG_PREFIX.name=SERENA_QUERY
LOG_PREFIX.name=SERENA_FALLBACK
LOG_PREFIX.name=SERENA_PROBE
LOG_PREFIX.name=drift-audit:
LOG_PREFIX.name=CHECK_TRIAGE

---

## Label-repair contradiction policy (current branch)

The active poller loop uses `reconcile_managed_issue_labels` for current-wave
managed issues and logs `LABEL_REPAIR*` diagnostics. The richer
contradiction-evidence helpers in `scripts/orchestrate_lib.py`
(`parse_phase_failure_markers`, `choose_most_advanced_conclusive_evidence`,
`resolve_label_repair_evidence`) are contract/reserved and not yet wired
into poller reconciliation.

---

## Reference

Operator runbooks (env var reference, autofix retrigger/dedup internals,
orchestrator integration-sync auto-heal, validation self-healing, workflow
log analysis pipeline, semantic cache scope, wrapper pin policy) live in
`./probably_unnecessary_but_read_if_stuck.md`. Read it only when needed —
it is intentionally large.

## Review pipeline consolidator + ledger contract

- Review-pipeline helper stages are fail-open by contract. Floor rules, consolidator, parser, and ledger failures degrade to empty/advisory local artifacts and do not block the editor or reviewer loop.
- `reviewer_bundle.txt` is the authoritative findings source. `review_issues.txt` and `ledger_status.txt` are advisory only and may not suppress valid raw-bundle findings.
- `floor_tags.txt` is the only non-skippable advisory channel: findings promoted there must be fixed or explicitly rejected with reason.
- The consolidator never gates. Empty `consolidator_raw.txt`, parser failure, uncovered anchors, or malformed prior-ledger state must not stop review/autofix.
- Editor prompts use the grep-friendly override convention `CONSOLIDATOR_OVERRIDDEN: <issue_id> — <reason>` inside the "Ignored suggestions" section when the editor intentionally rejects advisory consolidator guidance. Use `no-issue-id` when the parsed advisory issue has no stable id.
- Ledger identity is per-PR and stable across iterations via `REVIEW_LEDGER_PATH`. Status contract: `NEW`, `PERSISTING`, `FIXED`, `RESURGENT`, `accepted-residual`.
- `REVIEW_LEDGER_PERSIST_LIMIT` controls the `PERSISTING -> accepted-residual` transition. Once the threshold is reached, `review_issues.txt` is rewritten to residual stubs while the durable ledger retains the full history.
- The ≥2-reviewer floor rule is non-overridable at classification time: `scripts/review_floor_rules.sh` promotes same-file, nearby findings from distinct reviewers into `FLOOR_MULTI_REVIEWER`, and those tags remain non-skippable even if the consolidator down-ranks the issue.
- The review-autofix reviewer pass remains model-diversity-first. The consolidator's seven lenses are this repo's equivalent of Cloudflare's seven specialised review sub-agents; the pipeline does not run one fixed model per lens.
- Reviewer prompts now carry explicit anti-rules in both `prompts/review-reviewer-checklist.txt` (`WHAT NOT TO FLAG` under each lens) and the shared `COMMON ANTI-RULES` block rendered by `scripts/review_run_reviewers.sh`.
- `scripts/review_run_reviewers.sh` can classify a PR into `trivial | lite | full` reviewer tiers from reviewer-visible diff LOC/file counts, with `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX` forcing `full` on sensitive paths. Default tier fan-out follows the live `REVIEWER_MODELS` order from `.github/workflows/review_autofix.yml`: trivial = first reviewer, lite = first two reviewers, full = the complete configured set.
- `scripts/review_filter_uninteresting_files.sh` strips low-signal lock/generated/minified paths before reviewer fan-out and emits `REVIEWER_FILTER_SKIP: <path> <reason>` for each skipped file. Default exemptions remain `db/contracts/**`, `**/migrations/**`, and `**/migrate/**`.
- `scripts/review_agents_md_materiality.sh` is deterministic-path-glob v1: it writes a JSON result payload plus a non-blocking PR comment headed `## AI Materiality Advisory` when materiality is `high` or `medium` and root `agents.md` is unchanged. `AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED` is reserved only; enabling it still does not trigger a model call in the current shipped script.
- `REVIEW_LEDGER_REREVIEW_ENABLED` gates consolidator-side suppression of repeated `accepted-residual` / `won't-fix` findings from the existing review ledger and the review-blocked judge's ledger-fed prior-round decision input. `scripts/review_rb_judge.sh` renders that `=== BEGIN PRIOR ROUND DECISIONS ===` block via `render_review_rb_prior_round_decisions_file`, and `prompts/mode-judge-review-blocked.txt` treats it as advisory history rather than fresh reviewer evidence.
- `REVIEWER_CIRCUIT_BREAKER_ENABLED` persists reviewer health under `.ai/review_runtime/pr-<PR>/reviewer_health_state.json`. Retryable reviewer failures first retry with cheaper reasoning, then consult `scripts/reviewer_failback_chains.json`; unmapped reviewers fail open via `REVIEWER_FAILBACK_UNMAPPED`. The current mapping file covers `deepseek/deepseek-v4-pro -> deepseek/deepseek-v3.2`, `qwen/qwen3.6-plus -> qwen/qwen3-coder-plus`, and `x-ai/grok-4.20 -> x-ai/grok-4.1-fast`; `minimax/minimax-m2.5`, `moonshotai/kimi-k2.5`, and `mistralai/mistral-small-2603` remain intentionally unmapped until the catalog ships same-family alternates.
- `scripts/cost_audit.py` now parses additive review telemetry fields `cache_hit_rate`, `wall_clock_p50_ms`, `wall_clock_p99_ms`, `break_glass_count`, and `context_budget_warn_count`. `CONTEXT_BUDGET_WARN` is emitted pre-flight from review / consolidator / judge paths when a prompt exceeds the configured per-model context threshold.
- `scripts/codex_heartbeat.sh` wraps long-running `codex exec` calls in reviewer, consolidator, review-blocked judge, conflict-resolver, and validate/self-heal paths, emitting `CODEX_HEARTBEAT: phase=<phase> elapsed_secs=<n>` during silent periods.
- `REVIEW_APPROVAL_RUBRIC_ENABLED` lets the review-blocked judge emit logical `review_state` values (`APPROVE`, `APPROVE_WITH_COMMENTS`, `COMMENT`, `REQUEST_CHANGES`) that `scripts/post_review_comment.sh --review-state` maps to outbound PR reviews. With `REVIEW_BREAK_GLASS_ENABLED`, a human comment anchored as `@codex break-glass` downgrades only the outbound `REQUEST_CHANGES` event to comment-only and logs `BREAK_GLASS`, while preserving the judge's written review body.
- `scripts/pr_checks_lib.sh` is the single source of truth for the PR check-runs merge gate (`_pr_checks_completed` + `_pr_required_check_names_for_base`). Both `scripts/orchestrate_poll_process.sh` (the final integration-merge gate **and** all four review-blocked merge gates) and `scripts/review_rb_judge.sh` (the standalone judge's `merge_with_followup` gate) source it, so the required-checks filter (branch protection ∪ `ORCH_FINAL_MERGE_REQUIRED_CHECKS`, with `*`=block-on-any and `""`=allow-all sentinels) can never drift between paths. Pending check-runs always block; a FAILED check-run blocks only when its name is in the required set, so a non-required/environmental red (e.g. CodeQL when code scanning is disabled) no longer deadlocks a judge-approved review-blocked merge. The library carries the `_is_self_check_run` exclusion (gated by `PR_CHECKS_SELF_RUN_ID`) the rb_judge needs to skip its own in-progress host job; the orchestrator leaves it unset so the exclusion is a no-op there. `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` is declared in both the library (set-if-unset) and `orchestrate_poll_process.sh` (a fail-safe so a sourcing failure can never reach the empty-string allow-all sentinel); `tests/test_pr_checks_lib_required_filter.py` pins the two literals equal. The library is staged into `SUPPORT_SCRIPTS_DIR`/`scripts/` everywhere the two callers run (`REQUIRED_BOOTSTRAP_SCRIPTS`, `orchestrate_poll.yml`, `review_autofix.yml`); a missing library leaves `_pr_checks_completed` undefined and every gate fails closed (no merge) — the safe direction.

### AGENTS.md materiality classifier (deterministic v1)

| Materiality | Deterministic rule set in `scripts/review_agents_md_materiality.sh` | Advisory when root `agents.md` is unchanged? |
|---|---|---|
| `high` | Root manifests (`package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`); `.github/workflows/**`; root build/test config files (`pytest.ini`, `tox.ini`, `jest` / `vitest` / `playwright` / `cypress` / `webpack` / `vite` configs, `turbo.json`, `go.work`); newly added top-level directories detected against `origin/$BASE_BRANCH` | Yes |
| `medium` | Dependency / lock manifests (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `poetry.lock`, `uv.lock`, `go.sum`, `Pipfile*`, `requirements*.txt`, `constraints*.txt`); lint / format configs (`.eslintrc*`, `eslint.config.*`, `.prettierrc*`, `stylelint*`, `ruff`, `flake8`, `pylintrc`, `biome`); API-client wrapper paths such as `sdk/client.go`, `apis/client.ts`, or `*_client.*` | Yes |
| `low` | Paths that match none of the deterministic `high` / `medium` rules | No |

| Variable | Default | Contract |
|---|---|---|
| `REVIEW_FLOOR_RULES_ENABLED` | `1` | Enable floor-rule tagging before the editor runs. |
| `REVIEW_FLOOR_KEYWORDS_FILE` | `(empty)` | Optional keyword catalog override; empty / missing / unreadable falls back to the built-in catalog. |
| `REVIEW_CONSOLIDATOR_ENABLED` | `1` | Enable the advisory consolidator stage. |
| `REVIEW_CONSOLIDATOR_MODEL` | `openai/gpt-5.4` | Default consolidator model in `review_autofix.yml`. |
| `REVIEW_CONSOLIDATOR_REASONING` | `xhigh` | Default consolidator reasoning effort. |
| `REVIEW_CONSOLIDATOR_TIMEOUT_SECS` | `300` | Default consolidator timeout in seconds. |
| `REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT` | `16000` | Default consolidator output-token budget. |
| `REVIEW_PARSER_FAILOPEN` | `1` | Keep parser failures advisory instead of fatal. |
| `REVIEW_LEDGER_ENABLED` | `1` | Enable per-PR ledger persistence and `ledger_status.txt` emission. |
| `REVIEW_LEDGER_PERSIST_LIMIT` | `2` | Threshold for the `accepted-residual` transition. |
| `REVIEW_LEDGER_PATH` | `.ai/review_issue_ledger/pr-${PR_NUMBER}.txt` | Default per-PR ledger path. |
| `REVIEW_REVIEWER_CHECKLIST_ENABLED` | `1` | Append the reviewer checklist block when the prompt template is available. |
| `REVIEW_REVIEWER_ITERATION_SCOPING` | `1` | Scope later reviewer passes from last-run changed files plus actionable ledger rows; first pass stays full-diff. |
| `REVIEW_LEDGER_REREVIEW_ENABLED` | `false` | Enable ledger-aware re-review suppression in the consolidator and the review-blocked judge's prior-round-decision input. |
| `REVIEW_APPROVAL_RUBRIC_ENABLED` | `false` | Enable logical review-state output from the review-blocked judge and outbound PR-review mapping through `post_review_comment.sh --review-state`. |
| `REVIEW_BREAK_GLASS_ENABLED` | `false` | Enable the anchored `@codex break-glass` override scan; when active it downgrades only the outbound `REQUEST_CHANGES` event to comment-only. |
| `REVIEWER_RISK_TIER_ENABLED` | `0` | Enable deterministic `trivial | lite | full` reviewer fan-out by reviewer-visible diff LOC/file count. |
| `REVIEWER_RISK_TIER_TRIVIAL_LOC` | `10` | Trivial-tier LOC threshold. |
| `REVIEWER_RISK_TIER_TRIVIAL_FILES` | `20` | Trivial-tier changed-file threshold. |
| `REVIEWER_RISK_TIER_LITE_LOC` | `100` | Lite-tier LOC threshold. |
| `REVIEWER_RISK_TIER_LITE_FILES` | `20` | Lite-tier changed-file threshold. |
| `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX` | sensitive-path regex | Force full reviewer fan-out on matching paths (default matches `scripts/`, `.github/workflows/`, `.github/ai/`, `prompts/`, `workflow-templates/`, `db/contracts/`, and `ai-memory/`). |
| `REVIEWER_TIER_TRIVIAL_MODELS` | `(empty)` | Optional comma-separated trivial-tier subset; empty falls back to the first live reviewer model from `REVIEWER_MODELS`. |
| `REVIEWER_TIER_LITE_MODELS` | `(empty)` | Optional comma-separated lite-tier subset; empty falls back to the first two live reviewer models from `REVIEWER_MODELS`. |
| `REVIEWER_FILTER_UNINTERESTING_ENABLED` | `false` | Enable pre-review stripping of low-signal lock/generated/minified files before reviewer fan-out. |
| `REVIEWER_FILTER_EXTRA_GLOBS` | `(empty)` | Optional comma-separated extra skip globs for `review_filter_uninteresting_files.sh`. |
| `REVIEWER_FILTER_EXEMPT_GLOBS` | `db/contracts/**,**/migrations/**,**/migrate/**` | Comma-separated exemption globs that stay reviewer-visible even when they match a skip rule. |
| `REVIEWER_CIRCUIT_BREAKER_ENABLED` | `0` | Enable per-reviewer health-state caching and same-family failback attempts. |
| `REVIEWER_FAILBACK_MAX_RETRIES` | `1` | Retryable-failure budget before a reviewer slot consults the failback chain. |
| `REVIEWER_HEALTH_OPEN_THRESHOLD` | `3` | Consecutive retryable failures required to mark a reviewer slot `open` in the health cache. |
| `REVIEWER_HEALTH_OPEN_TTL_SECS` | `1800` | Seconds an `open` reviewer-health entry suppresses dispatch before automatic expiry. |
| `AGENTS_MD_MATERIALITY_ENABLED` | `1` | Post the deterministic, non-blocking AGENTS.md materiality advisory when a material change omits an agents.md update (on by default; set `0` to disable). |
| `AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED` | `0` | Reserved only; deterministic v1 still makes no materiality model call when this flag is on. |
| `AGENTS_MD_MATERIALITY_MODEL` | `openai/gpt-5.4-mini` | Reserved future materiality fallback model slug. |
| `AGENTS_MD_MATERIALITY_REASONING` | `medium` | Reserved future materiality fallback reasoning effort. |
| `CONTEXT_BUDGET_WARN_RATIO` | `0.7` | Per-model context-window ratio above which review-surface prompt builders emit `CONTEXT_BUDGET_WARN`. |
| `MAX_PROMPT_TOKENS_FOR_PHASE` | `(empty)` | Absolute prompt-token override that takes precedence over `CONTEXT_BUDGET_WARN_RATIO`; phase-specific `MAX_PROMPT_TOKENS_FOR_<PHASE>` overrides remain supported. |
| `CODEX_HEARTBEAT_ENABLED` | `1` | Enable the `codex_heartbeat.sh` wrapper on long-running review / validate Codex calls. |
| `CODEX_HEARTBEAT_INTERVAL_SECS` | `30` | Silence interval (seconds) between emitted `CODEX_HEARTBEAT` lines. |

## Integration-sync verifier + bootstrap contract

- `scripts/verify_integration_fingerprints.py` supports `--baseline-fingerprints-state <out>` / `--compare-against-baseline <in>` alongside `--ref`; capture mode records ref-accurate `head_sha` metadata, compare mode emits `PRE_EXISTING_FINGERPRINT_DRIFT_V1` markers for pre-existing drift that should not block the resolver commit, and the verifier-side false-positive defenses emit `FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1` (capture-side multi-occurrence partial removal), `FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1` (a `must_contain` line modified after capture by a non-`[ai-merge-resolve]` commit), and `FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1` (a `must_not_contain` line re-added after capture by a non-`[ai-merge-resolve]` commit — e.g. a back-merge of the default branch keeping its still-present copy) when the ref-mode wave-dispatch gate suppresses a non-resolver false positive. The two post-capture defenses share one direction-agnostic pickaxe primitive and both fail closed in working-tree mode, so the resolver's own pre-commit self-check stays strict and still cannot silently revert merged intent.
- `.github/workflows/review_autofix.yml` stages `verify_integration_fingerprints.py`, `review_conflict_prepare.sh`, and `review_conflict_resolve.sh` through `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` (main snapshot first, branch fallback). `OPTIONAL_BOOTSTRAP_SCRIPTS` is reserved for genuinely optional helpers only.
- `scripts/review_conflict_resolve.sh` persists one `AUTOFIX_RESOLVER_RETRY_STATE_V1` PR-body block per final PR/head SHA, keyed by normalized fingerprint failure signature. `RESOLVER_ESCAPE_THRESHOLD_N` is the per-tier same-head, same-signature step size: multiples advance `strict` → `ratio` → `count_only` → `warn_only`, emit `FINGERPRINT_TIER_DOWNGRADED_V1`, and after the next multiple the script labels the **final PR issue** `ai:resolver-escalated` and records `escalated_at` for poller-side suppression / branch-rebuild gating.
- `scripts/verify_integration_fingerprints.py` uses `FINGERPRINT_QUARANTINE_RUNS_M` to move stable unchanged drift into ai-memory quarantine and emits `FINGERPRINT_QUARANTINED_V1` markers when the skip path activates. `.github/workflows/drift-audit.yml` (cron `0 3 * * *`, gated by `DRIFT_AUDIT_ENABLED`) scans `PRE_EXISTING_FINGERPRINT_DRIFT_V1` / `FINGERPRINT_QUARANTINED_V1` markers and maintains tracker issues for persistent clusters. The audit skips any cluster whose fingerprint path is absent from the repository checkout, so markers echoed from test fixtures or PR diffs (synthetic paths such as `scripts/example.py`) do not open tracker issues. Every enabled run posts a Telegram run summary (`tg_send_msg`, gated by `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID`) linking to the run and writes a GitHub Actions job summary.
- `scripts/orchestrate_poll_process.sh` gates last-resort `orchestrator/project-*` branch rebuilds behind `BRANCH_REBUILD_ENABLED`, `BRANCH_REBUILD_THRESHOLD_HOURS`, and `BRANCH_REBUILD_COOLDOWN_HOURS`. Audit snapshots are persisted as `BranchRebuildAuditV1` in `ai-memory/schemas/branch_rebuild_audit.v1.json` (this shipped artifact supersedes the old plan placeholder name `BRANCH_REBUILD_AUDIT_V1`; there is no literal runtime marker with that string).

## Operational lessons learned (categorised)

**General / Tooling**
- Treat the `openai/codex#11151` no-edit regression as closed only with function-style patch tooling; keep `apply_patch_tool_type = "function"` as the settled baseline. Pointers: `scripts/codex_model_catalog.json`, `scripts/write_codex_config.sh`.
- Keep `low` as the default gpt-5.4 verbosity across workflow entrypoints unless a specific phase re-proves the old announce-without-emit failure. Pointers: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate.yml`.

**codex-cli quirks**
- Announce-without-emit is a known codex-cli failure mode on patch-heavy turns; the mitigation is to keep patch tooling explicitly enabled rather than raising verbosity by default. Pointers: `scripts/codex_model_catalog.json`, `.github/workflows/implement.yml`.
- The OpenRouter Responses-path regression was tied to `apply_patch_tool_type: "freeform"`; keep `include_apply_patch_tool = true` and function-style patch wiring in editor phases. Pointers: `scripts/codex_model_catalog.json`, `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`.

**OpenRouter / prompt-cache**
- Prompt-cache hit rate depends on stable prompt ordering and unchanged prefix blocks; preserve cache-friendly layout before adding new dynamic material. Pointers: `probably_unnecessary_but_read_if_stuck.md` (OpenRouter Prompt Cache Instrumentation / Semantic Cache Scope), `scripts/openrouter_prompt_cache.py`.
- `OPENROUTER_PROMPT_CACHE_DISABLED` is the explicit kill switch, and Gemini-family models may skip cache breakpoints when the reviewer path marks them incompatible. Pointers: `probably_unnecessary_but_read_if_stuck.md`, `scripts/review_run_reviewers.sh`.

**GitHub API rate-limits**
- Shared GitHub quota handling is reset-aware: use the repo helpers' `gh_retry` backoff behavior instead of ad-hoc retry loops. Pointer: `scripts/gh_helpers.sh`.
- Rate-limit alerting is deduplicated by pin/cooldown state, and repeated issue/PR lookups should flow through the poller's batched GraphQL helpers. Pointers: `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`.

**Memory subsystem**
- The `ai-memory` branch is the canonical backing store; consumers must fail open when memory reads or writes are unavailable. Pointers: `scripts/memory_helpers.sh`, `scripts/ai_memory.py`.
- `AI_MEMORY_TELEMETRY` and the per-PR review ledger are continuity surfaces, not hard gates; preserve ledger identity across reruns. Pointers: `scripts/ai_memory.py`, `scripts/review_issue_ledger.sh`.

**Validation harness Docker lifecycle**
- Validation containers distinguish `/bin/sh -c` from `/bin/sh -lc`; shell choice is part of harness correctness, not a cosmetic variation. Pointers: `scripts/validation_lint.py`, `prompts/mode-validate-generate.txt`.
- npm/yarn/pnpm wrapper shutdown handling and `mongosh` apt-repo constraints are harness invariants; keep the existing SIGTERM/exit-code and package-source rules intact. Pointers: `scripts/validate_driver.sh`, `prompts/mode-validate-fix-harness.txt`.
