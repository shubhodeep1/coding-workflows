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
   model reviewer + consolidator + editor loop on PR changes. Two pre-review
   gates run first: the merge train (`scripts/review_merge_train.sh gate`,
   `MERGE_TRAIN_ENABLED`) queues an `ai/issue-*` PR behind older open
   `ai/issue-*` PRs on the same base that edit the same files (label
   `ai:merge-queued`; released by `cancel_on_pr_close.yml` on close and by
   `orchestrate_poll.yml` every tick; managed/standalone conflict and stall
   recovery treat the label as an intentional wait), and the merge-topology gate hands a
   content conflict to the resolver tail *before* the reviewer/editor spend
   (`PRE_REVIEW_CONFLICT_RESOLVE_ENABLED`, sets `AUTOFIX_PRE_REVIEW_RESOLVE`).
8. **conflict resolver** (`prompts/conflict-resolver.txt`,
   `integration-sync-conflict-resolver.txt`) — merge-conflict resolution
   inside autofix. In consumer repos the resolver, the review-blocked judge
   and the poller remove workflow-generated root files before committing but
   keep any path HEAD tracks (a consumer-owned `agents.md`), logging
   `Preserving repo-tracked path during artifact cleanup: <path>`.
9. **orchestrate** (`orchestrate.yml`, `orchestrate_poll.yml`) — issue
   decomposition + judge polling, including the default-on, current-head
   project security-pass gate before validation/finalization.
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

Planner scope note: the Boil the Lake rule is a planner-side instruction for
choosing the right scope mode up front, while CLAUDE.md §5 / the unattended
minimal-change rules still bind implementers and reviewers after that choice is
made. There is no conflict: planners may explicitly choose Expansion or
Selective Expansion when the marginal completeness cost is small, and editors /
reviewers must then stay surgical inside that approved scope rather than
shrinking it ad hoc.

CI guard contract: `.github/workflows/ci.yml` runs the `Shared shell-block
anti-regression checks` step immediately after checkout, before Python setup,
dependency installation, lint, and tests. Rejections emit the secret-safe
`CI_GUARD_FAILURE` diagnostic with `guard`, `check`, `file`, `line`,
`expected`, and `scanned_files` fields, and the guard deliberately uses
ubiquitous `grep` instead of `rg` so runner images without ripgrep still fail
only on real policy drift.

Integration-ref trust boundary: `scripts/resolve_integration_ref.sh` can return
any existing valid Git branch name declared by issue metadata. Workflows may
pass that output to action inputs or through step-local environment variables,
but must never interpolate it directly into `run:` script source.
`tests/test_workflow_checkout_integration_ref_audit.py` pins the env-bound log
contract for every resolver-consuming workflow.

Plan prompt note: `PLAN_DIAGRAMS_OPTIONAL` defaults to `true`, so plan outputs
may include `Data flow:`, `State machines:`, and `Failure modes:` only when
they materially help. Trivial plans should omit them, and `State machines:` is
required only for changes touching the orchestrator phase machine
(`ai:clarification` → `ai:planning` → `ai:awaiting-approval` →
`ai:implementing` → `ai:done` → `ai:ready-to-merge` → `ai:merged`).

Prompt assembly note: the migrated shared-prelude prompt family keeps the
legacy runtime bodies in `prompts/mode-*.txt` and stores the include-based
sources in `prompts/_templates/*.txt`. `scripts/assemble_prompt.sh` is a thin
wrapper over `render_prompt.py --assemble-only`, and `scripts/render_prompt.sh`
uses it only when `PROMPT_PRELUDE_REFACTOR_ENABLED=true`. Default persona
prefixes come from the checked-in JSON map at `prompts/_prelude_role_persona.txt`
unless `PROMPT_PERSONA_PREFIX_ENABLED` is disabled.

## Plan Decisions advisory lint

- `scripts/lint_plan_decisions.py` is the fail-open linter for the plan
  `## Decisions` convention under `docs/plans/*.md`. It always exits `0`,
  emits advisories to stderr, and treats missing or malformed decision records
  as warnings rather than merge blockers.
- `.github/workflows/ci.yml` runs the linter in the
  `Plan decisions advisory lint` step with `continue-on-error: true`.
- `DOCS_DECISION_LINT_ENABLED` (default `false`) only controls whether CI
  replays captured advisories into the job log and `GITHUB_STEP_SUMMARY`; it
  does not disable the underlying linter run.
- The documented plan schema is `## Decisions` containing one or more
  `### D<n> — <title>` records with `Chosen`, `Alternatives considered`, and
  `Why` bullets.

## Stable-ID convention

- New AI-pipeline identifiers must be created through the canonical helper
  `make_record_id(prefix)` in `scripts/ai_memory_lib.py`; do not hand-roll
  new ID formats alongside it. The Phase 5 plan's
  `scripts/ai_memory_lib.py:480` pointer is historical; follow the live
  `make_record_id(prefix)` definition in that file.
- The current format is contractual: `<prefix>_<YYYYMMDDHHMMSS>_<10hex>`.
  The timestamp is UTC and the suffix is the first 10 lowercase hex characters
  from a UUID4.
- `make_record_id(prefix)` sanitizes the caller-supplied prefix through
  `sanitize_segment(prefix, "mem")` before composing the ID. Today that
  preserves ASCII letters, digits, and `_.:-`, replaces other runs with `_`,
  trims leading/trailing `.` or `_`, preserves existing safe prefixes such as
  `mem` and `run_event`, and falls back to `mem` for empty or invalid-only
  inputs.
- Example shapes: `mem_20260709041614_1f025339dd` and
  `run_event_20260709041614_1f025339dd`.
- This format is a compatibility contract. If a future change needs a new
  stable-ID format, follow the §6 backward-compatibility rule: keep
  `make_record_id(prefix)` emitting the current format, introduce the new
  format via an alongside helper/alias so old IDs remain valid and existing
  outputs stay stable, and update the contract test in the same change.

## Override conventions

Two override files let maintainers pin specific catalog fields
or prompt files as untouchable by future regenerators:

- `scripts/codex_model_catalog_overrides.yaml` — per-model-slug
  field overrides for `scripts/codex_model_catalog.json`. The
  fields listed under each slug's `overrides:` block are merged
  over the catalog defaults at render time by
  `scripts/generate_codex_model_reference.py` and emitted with a
  `(frozen)` marker in the generated `docs/codex-model-reference.md`.

- `prompts/.overrides.yaml` — per-file freeze markers for any
  prompt file under `prompts/`. Files listed with
  `skip_validation: true` are excluded from any future automated
  prompt-rewriter tooling.

§6 implication: overrides are the canonical way to preserve
intentional non-default values across future regenerations
without renaming or removing the underlying identifier. To freeze
a new value, add it to the appropriate overrides file with a
`notes:` / `reason:` block explaining why.

## Utility helpers

- `scripts/repo_root.py` provides `repo_root()` and
  `repo_root_from(start: Path)` for scripts and tests that need to
  resolve the repository root by walking upward until they find both
  `CLAUDE.md` and `.git/`.
- `scripts/generate_resource_id.py` provides
  `generate_id(prefix, salt=None)` as a wrapper around the live
  `make_record_id(prefix)` helper in `scripts/ai_memory_lib.py`.
  Unsalted calls delegate to `make_record_id(prefix)` unchanged;
  salted calls preserve the same `<prefix>_<UTC timestamp>_<10 hex>`
  shape while deriving the suffix from
  `sha256(salt.encode("utf-8"))`, including the empty-string salt.

## Implement scope-lock label

- When `SCOPE_LOCK_LABEL_ENABLED=true`, `implement.yml` recognizes one active
  dynamic issue label of the form `ai:scope:<glob>` and copies the glob into
  the implementation context.
- The glob follows Bash `globstar` semantics (for example `scripts/**/*.py` or
  `prompts/mode-*.txt`) and is enforced after the local AI commit is created
  but before push: `scripts/implement_commit_changes.sh` reuses
  `scripts/files_touched_scope_guard.py` against the committed pathset and
  rolls the unpushed local commit back on any out-of-glob path.
- Multiple `ai:scope:` labels are not merged; the workflow enforces only the
  first matching label from the issue metadata and logs a warning.
- This is a runtime dynamic-label exception to the otherwise static
  `.github/ai/label_contract.v1.json` contract. The glob-bearing
  `ai:scope:<glob>` labels are intentionally not enumerated there and are not
  part of contract-driven label repair.


## Implement self-repo staged-support ledger

- `implement.yml` stages the runtime helpers *in-tree* (`install -m … "scripts/${f}"`,
  `prompts/*`, `ai-memory/schemas/*`) from `SCRIPT_REF`, which in this repository is
  `github.sha` (the default branch) while the checkout may be an orchestrator
  integration branch. Those installs overwrite tracked files, and the self-repo
  commit path in `scripts/implement_commit_changes.sh` has no `scripts/` exclusion.
- The staging step records every tracked file it overwrote in
  `STAGED_SUPPORT_LEDGER` (`${RUNTIME_DIR}/staged_support_overwrites.txt`) with the
  installed content under `STAGED_SUPPORT_BASE_DIR`; it also records support
  paths recreated over branch-side deletions. Executable support-ref copies live
  under `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`. All three paths are exported through
  `GITHUB_ENV` and only exist when `github.repository` is this repository.
- `scripts/implement_commit_changes.sh` consumes the ledger before `git add`:
  restore-to-HEAD for untouched copies, 3-way `git merge-file` re-base for
  editor-edited copies, preserve branch/editor deletions, remove untouched
  staging recreations, and fail closed on incomplete inputs or merge conflicts.
  The workflow invokes the immutable runtime copy of the commit helper and uses
  runtime copies for all later helper calls, so restoring the worktree cannot
  replace the running script or downgrade post-commit tooling.
  Log keys: `IMPLEMENT_STAGED_SUPPORT_LEDGER`, `IMPLEMENT_STAGED_SUPPORT_RESTORED`,
  `IMPLEMENT_STAGED_SUPPORT_REBASED`, `IMPLEMENT_STAGED_SUPPORT_DELETED_BY_EDITOR`,
  `IMPLEMENT_STAGED_SUPPORT_RECREATED_BY_EDITOR`, `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING`,
  `IMPLEMENT_STAGED_SUPPORT_HEAD_READ_FAILED`, `IMPLEMENT_STAGED_SUPPORT_REBASE_CONFLICT`,
  `IMPLEMENT_STAGED_SUPPORT_REBASE_FAILED`, `IMPLEMENT_STAGED_SUPPORT_RESTORE`.
- Staged-support failures are consumed by the runtime-preserved rejection handler,
  which applies `ai:needs-human`, comments with the affected paths, sends the
  configured CRITICAL alert, and prevents generic diagnosis/re-issue handling.
- The other in-tree staging workflows (`clarify.yml`, `plan.yml`,
  `orchestrate_clarify_respond.yml`, `orchestrate.yml`, `orchestrate_poll.yml`,
  `check_failure_triage.yml`) either never commit from that checkout or run on
  the same ref they stage from, so the ledger is wired into `implement.yml` only.
  `review_autofix.yml` and `validate.yml` stage out of tree via
  `scripts/stage_workflow_support.sh`.
- Incident: implement runs 34392788763 (PR #4071) and 34613019339 (PR #4079)
  committed `main`'s copies of eight helpers onto `orchestrator/project-3965`,
  reverting the branch's security-pass fixes. `tests/test_implement_post_codex_recovery.py`
  pins the ledger, immutable-execution, deleted-path, handler, and restore outcomes.

---

## Models in use (defaults; overridable via repo-vars)

| Phase | Default model | Default reasoning | Verbosity |
|---|---|---|---|
| clarify, clarify-respond | `openai/gpt-5.6-sol` | `xhigh` (smoke: `low` — `clarify.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` |
| plan | `openai/gpt-5.6-sol` | `xhigh` (smoke: `low` — `plan.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` |
| orchestrate (decompose), judge | `openai/gpt-5.6-sol` | `xhigh` | `low` |
| implement (main editor) | `openai/gpt-5.6-sol` | `xhigh` (smoke: no override — see `.github/workflows/implement.yml:597-606`) | `low` |
| implement-repair, implement-repair-syntax | `openai/gpt-5.6-sol` | `xhigh` | `low` |
| implement-diagnose | `openai/gpt-5.6-sol` | `xhigh` | `low` |
| review autofix editor | `openai/gpt-5.6-sol` | `xhigh` (smoke: `medium`) | `low` |
| review autofix reviewers (pass 1) | `REVIEWER_MODELS` (default roster: `minimax/minimax-m3`, `moonshotai/kimi-k3`, `deepseek/deepseek-v4-pro`, `mistralai/mistral-small-2603`, `qwen/qwen3.7-plus`, `x-ai/grok-4.6`) | `xhigh` per reviewer call (hardcoded at the `run_reviewer_pass ... "xhigh"` callsite in `scripts/review_run_reviewers.sh:4733`; not affected by the smoke `REVIEWER_REASONING_EFFORT=low` override in two-pass mode) | `low` |
| review autofix reviewers (pass 2) | `REVIEWER_MODELS` (same roster, after pass-2 scope / tier filtering) | `high` on diffs below `REVIEWER_PASS2_DIFF_LARGE_LOC=200`, `xhigh` at or above that threshold; smoke: `low`; operator override wins | `low` |
| review consolidator | `openai/gpt-5.6-sol` | `xhigh` | `low` |
| conflict resolver | `openai/gpt-5.6-sol` | `high` (decoupled from smoke; `scripts/review_conflict_resolve.sh` validates `xhigh`, `high`, `medium`, `none` only — `low` is rejected; default lowered from `xhigh` after runs `25627236793` / `25627316961` hit `timeout`-killed retries on degenerate orchestrator-stack integrations; override per-repo via `vars.THINKING_LEVEL_CONFLICT_RESOLVER`) | `low` |
| validate generate, diagnose | `openai/gpt-5.6-sol` | `xhigh` | `low` |
| validate discover | `openai/gpt-5.6-sol` | `xhigh` (per-phase override via `MODEL_REASONING_EFFORT_DISCOVER`) | `low` |
| validate fix-harness, self-heal | `openai/gpt-5.6-sol` | `xhigh` | `low` |
| workflow log analyze | `openai/gpt-5.6-sol` | `xhigh` | `low` |
| workflow audit | `openai/gpt-5.6-sol` | `xhigh` (hardcoded in `.github/workflows/workflow-log-analysis.yml:716-717`) | `low` |
| workflow api-redundancy | `openai/gpt-5.6-sol` | `xhigh` (default of `THINKING_LEVEL_ANALYSIS`) | `low` |
| workflow log summary | `openai/gpt-5.6-luna` | default | `low` |
| reviewer consensus summariser | `openai/gpt-5.6-luna` | `medium` (`XPOLL_SUMMARISER_REASONING`) | `low` |

OpenCode version `1.18.23` is installed by the dispatch-only
`.github/workflows/opencode-live-smoke.yml` rollout gate and by production
`review_autofix.yml`, which warms the models.dev cache before the complete
review model pipeline runs. Reviewer, summariser, interim-judge,
behavioural-smoke synthesiser, editor, review-blocked judge/fix, consolidator,
and conflict-resolver calls use isolated OpenCode configurations. OpenCode
failures do not fall back to Codex; compatibility
`CODEX_*` identifiers and watchdog helper names remain unchanged. Other
production phases remain Codex-backed until their cutovers land.

<!-- Historical Phase 1 rollout snapshot retained for integration fingerprint verification:
OpenCode Phase 1 is operationally inert: version `1.18.23` is installed only
by the dispatch-only `.github/workflows/opencode-live-smoke.yml` rollout gate.
All production phases, including review/autofix, remain Codex-backed until a
later cutover phase lands after the all-slug live smoke succeeds.
-->

All gpt-5.6-sol phases now resolve to `low` verbosity at every layer: the per-phase
`MODEL_VERBOSITY` env-var default in `.github/workflows/*.yml` (`VERBOSITY_*`
repo-vars), the `-c model_verbosity=low` CLI flag on every `codex exec`
callsite (≈20 sites across `scripts/*.sh` and `.github/workflows/*.yml`),
the `model_verbosity = "low"` line that `scripts/write_codex_config.sh:242`
writes into `config.toml`, and the `"default_verbosity": "low"` for
`openai/gpt-5.6-sol` in `scripts/codex_model_catalog.json`. Third-party
reviewer models (`minimax/minimax-m3`, `moonshotai/kimi-k3`,
`deepseek/deepseek-v4-pro`, `mistralai/mistral-small-2603`,
`qwen/qwen3.7-plus`, `x-ai/grok-4.6`)
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

Every editor / consolidator / resolver phase now defaults to `openai/gpt-5.6-sol`.
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
models (`minimax/minimax-m3`, `moonshotai/kimi-k3`,
`deepseek/deepseek-v4-pro`, `mistralai/mistral-small-2603`,
`qwen/qwen3.7-plus`, `x-ai/grok-4.6`) plus
`unattended_system_instructions.md` as system context.

---

## Interactive slash-command model selection

**Interactive Claude Code sessions only.** This section is unrelated to
`## Models in use` above: that table covers the unattended codex/OpenCode
pipeline models, which read `unattended_system_instructions.md` and are
driven by repo-vars.

The 12 commands in `.claude/commands/*.md` deliberately carry **no**
`model:` frontmatter, and no `context:` / `background:` keys either. Every
`/command` runs on the model the operator picked for the session (via
`/model` or the session's configured model), for every turn of the command.
Do not re-add per-command `model:` pins: they were tried (PRs #3967 and
#3971) and removed because the operator's session choice should decide the
model, not the command file. A file that starts with `---` would be parsed
as frontmatter, so the command body must remain the first line.

No field here changes what any consumer repo receives on the `@stable`
sync: `.claude/commands/` is not part of the synced surface, and the
template copies under `workflow-templates/.claude/commands/` have never
carried frontmatter.

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
PROFILE.name=full manifest=workflow-templates/profiles/full.txt wrappers=ai-cancel-on-pr-close.yml,ai-check-failure-triage.yml,ai-clarify.yml,ai-implement.yml,ai-issue-pr-status.yml,ai-memory-maintenance.yml,ai-orchestrate-clarify-respond.yml,ai-orchestrate-poll.yml,ai-orchestrate.yml,ai-plan.yml,ai-review.yml,ai-security-audit.yml,ai-sync-labels.yml,ai-update-workflows.yml,ai-validate.yml,review_rb_judge_dispatch.yml

## Immutable consumer wrapper pins

- Canonical `workflow-templates/*.yml` files retain `@stable` as a delivery-time
  render token. Installed consumer wrappers must use
  `@<40-character-release-sha> # stable` instead.
- `scripts/workflow_wrapper_refs.py` is the single renderer used by the updater,
  drift audit, and `/seed-repo`; do not duplicate its substitution rules.
- `.github/workflows/update_workflows.yml` renders every top-level wrapper before
  any consumer mutation, updates an existing `ai-update-workflows.yml` regardless
  of profile, and never creates that self-updater when absent.
- Stable-release repository dispatch payloads carry both `version` and the peeled
  commit `sha`. Consumers validate the payload but independently resolve current
  `stable`, so delayed events cannot downgrade installed pins.

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

## Workflow scenario traces

- Flag: `WORKFLOW_LOG_SCENARIO_TRACE_ENABLED` (default `false`).
- Renderer: `scripts/render_scenario_trace.py` runs downstream of
  `workflow_log_collector.v2` inside `.github/workflows/workflow-log-analysis.yml`.
- Output: local-only `.ai/workflow_traces/<run_id>.scenario.json` files. The
  directory is gitignored and this phase does not upload or commit the traces.
- Schema: top-level `schema_version`, `run_id`, `phase`, and ordered `steps[]`
  with `schema_version: "workflow_scenario_trace.v1.json"`.
- Step types: `user_message` / `assistant_text` (`ts`, `content`, `tokens`),
  `tool_call` (`ts`, `name`, `args`), and `tool_result`
  (`ts`, `output`, `exit_status`).
- Parser markers are centralized in `scripts/render_scenario_trace.py` and
  currently key off `>>> [model]`, `<<< [model]`, `[tool_call]`, and
  `[tool_result]`.
- Logging: successful writes emit `WORKFLOW_SCENARIO_TRACE_WRITTEN`; fail-open
  per-run skips emit `WORKFLOW_SCENARIO_TRACE_PARSE_FAIL`.

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
- Token counts are harvested from the editor log named by `--tokens-log-file`.
  The helper reads only the trailing `LEDGER_TOKENS_LOG_MAX_BYTES` (default
  `1048576`, `0` disables the bound), because the `usage`-object scan probes
  every `{` in the document and `json.JSONDecodeError` counts newlines from
  byte 0 on each failed probe, making a whole-file scan quadratic in file
  size. Every parser keeps its last match and editors write their usage
  summary at the end, so the tail carries the same answer.
- The `record-run-event` write is bounded by `LEDGER_EMIT_TIMEOUT_SECONDS`
  (default `120`, `0` disables). It runs while the helper holds the
  per-dedupe-key `flock`, so an unbounded call would stall the caller's job
  silently. On expiry the helper warns and fails open without consuming the
  dedupe key.
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

When `ENABLE_SECURITY_PASS=true` (default `true`), every completion route
enters `security-pass` before validation or finalization. A pass is valid only
when `security_pass_status == "passed"` and `security_pass_head_sha` exactly
matches the current integration head. Findings enter `security-pass-fixing`
through one consolidated `ai:orchestrator-managed` issue (whose body asks the
implementer to clear every instance of each finding's defect class, not only
the cited line); a merged fix advances `security_pass_cycle`, clears the
recorded SHA, and re-runs the pass as a **delta audit**: the explicit range
stays `merge-base..head`, but `run_security_pass_inline` passes
`SECURITY_AUDIT_DIFF_SINCE=<security_pass_last_audited_sha>` so only range
files changed since the last audited commit stay in scope, plus the files
cited by `security_pass_reported_findings`, which travel to the engine as
`SECURITY_AUDIT_PRIOR_FINDINGS` (the engine re-emits persisting findings under
the same `finding_id` and reports remaining instances of the same class). Both
fields are written by the same `jq` that records `security_pass_head_sha`; a
clean pass empties the memory, `/re-security-pass` and the
`ENABLE_SECURITY_PASS=false` release path clear both. Legacy `passed` state
without the pointer falls back to `security_pass_head_sha`. A pointer that
does not resolve or is not an ancestor of the head falls back to the full
range; every choice is logged as `SECURITY_PASS_SCOPE tracking_issue=<N>
mode=full|delta reason=<no_prior_audit|head_advanced_since_last_audit|head_unchanged_since_last_audit|last_audited_sha_not_ancestor_of_head>
base_sha=<merge-base> since_sha=<sha|none> head_sha=<sha> prior_findings=<count>
waived_findings=<count>`.
Incident: tele-funtoken-msg-scoring#3928, fun-token-multi-chain#471 and
binance-blessings#249 each merged every fix issue, never repeated a finding ID
between cycles, and still exhausted the budget on fresh full-range samples.
Persistent findings after `MAX_SECURITY_PASS_CYCLES` (default `5`)
terminalize as `ai:security-pass-failed`; `/re-security-pass` resets the
bounded loop and the next audit covers the full range again.
The budget bounds *persistent* findings, so a recorded clean pass breaks the
chain: when the integration head advances past a `passed` SHA (a
`chore: sync <default> into <integration>` merge, a resolver/judge conflict
resolution, or a merged fix PR), `run_security_pass_inline` resets
`security_pass_cycle` to `0` before the re-audit and logs
`SECURITY_PASS_CYCLE_BUDGET_RESET ... reason=head_advanced_after_clean_pass`
with the tracking issue number and the new head SHA. Findings in the
newly-arrived code then get their own fix cycles instead of terminalizing the
project on sight. The reset fires only from a `passed` prior status; a
`blocked` chain keeps its spent budget. Completion still requires a clean
SHA-bound pass at the current head, so a fresh budget never admits unaudited
code.
Every security-pass transition that exits its current path or starts the long-running audit also keeps the tracking issue body honest:
`security_pass_fail_closed`, `security_pass_terminal_failure`,
`security_pass_closed_fix_failure`, the `blocked` write in
`create_security_pass_fix_issue`, the running, clean/pass, and
head-changed/pending writes in `run_security_pass_inline`, the stall-recovery
successor adoption, and the `/re-security-pass` reset call
`reconcile_tracking_body_after_security_pass_transition` between their
state write and `post_state_comment`, so the rendered
`<!-- orchestrator:security-pass -->` block matches the label and alert
comment the same transition posts and the persisted
`tracking_body_sync_hash` rides the state comment already being posted.
The tick-level reconcile sites run only on the `merge_conflict` and
wave-status paths and these security-pass paths leave the tick before reaching
them, which is why #3965 kept rendering `Status: passed` after it had failed.
The wrapper is hash-gated through
`reconcile_tracking_issue_body_from_state`: with `project_body_snapshot`
present, an unchanged body costs no API call; legacy state without the
snapshot first fetches the live issue body as its render template. A changed
body costs the single `gh issue edit`. It fails open.
A consolidated fix issue that orchestrator stall recovery closed and re-issued
is *not* a failed fix: `close_and_reissue` re-points wave state only (both
writes are gated on a non-null `local_id`, which a security-pass fix issue
never has), so before terminalizing, `resolve_security_pass_fix_successor`
looks for the live successor by the durable `- Tracking issue: #<N>` and
``- Local ID: `security-pass-fix-cycle-<K>` `` body markers that survive
re-issue, adopts it into `security_pass_active_fix_issues`, and logs
`SECURITY_PASS_FIX_ISSUE_SUCCESSOR_ADOPTED`. Both markers must match, so
another project or another cycle is never adopted. An inconclusive lookup
(API or parse failure) retains `security-pass-fixing` for retry rather than
reading a transient read failure as evidence of a failed fix.
Setting `ENABLE_SECURITY_PASS=false` remains the immediate operator kill switch.
Exhaustion is no longer terminal by default. When `MAX_SECURITY_PASS_CYCLES`
is spent with findings still open, `security_pass_exhaustion_judge` (gated by
`SECURITY_PASS_EXHAUSTION_JUDGE_ENABLED`, default `true`, and bounded by
`MAX_SECURITY_PASS_JUDGE_ROUNDS`, default `0` = unbounded) renders
`prompts/mode-judge-security-pass-exhaustion.txt` read-only against the
audited checkout and requires one decision per remaining finding:
`accept_with_followup` (waiver row in `security_pass_waived_findings`, id
dropped from `security_pass_reported_findings`, one non-blocking `ai:security`
follow-up issue recorded in `security_pass_followup_issues`), `keep_fixing`
(one more consolidated fix issue for exactly those findings), or `fail`
(the pre-judge `security_pass_terminal_failure`). A disabled judge, a missing
prompt, a codex failure, or an invalid verdict also fall back to the terminal
path; nothing passes silently. Every round increments
`security_pass_judge_rounds` (reset by `/re-security-pass` and the kill-switch
release) and posts a `⚖️ Security-pass exhaustion judge` comment with the
decision table. Waivers travel to the engine as `SECURITY_AUDIT_WAIVED_FINDINGS`
and `security_pass_apply_waivers_to_findings` re-applies them to the result
(exact id, or same file and category within `SECURITY_AUDIT_WAIVER_LINE_WINDOW`,
default 40 lines). `/security-pass-waive <finding_id> ...` (human
OWNER/MEMBER/COLLABORATOR only, dedup marker
`<!-- security-pass-waive-dedup:<comment-id> -->`) records operator waivers; in
the failed state it then resets the loop like `/re-security-pass`, in
`security-pass-fixing` it only persists. The scan runs before the
`security-pass-fixing` handler because that handler ends its tick. The
tracking body's `### Security pass` block renders `- Waived findings: N` only
when N > 0, so bodies without waivers are byte-identical.
If an externally merged final PR has already deleted its integration branch,
the only permitted analysis fallback is that PR's verified immutable head SHA;
an unavailable or mismatched PR head fails closed and never substitutes the
default-branch HEAD. If that audit returns findings, the poller recreates the
still-absent integration branch at exactly the verified PR-head SHA, accepts a
create race only when the authoritative branch SHA matches, and clears stale
final-delivery state before creating the fix issue. The repair then advances
the branch that the next security pass audits, and the existing finalizer opens
a replacement delivery PR; unknown or mismatched branch state fails closed.

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

The security-pass helpers update the same pinned comment to `waiting` while
the audit engine or consolidated fix issue gates completion, and to `failed`
when the bounded fix-cycle budget is exhausted.

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
- `WORKFLOW_SCENARIO_TRACE_WRITTEN`
- `WORKFLOW_SCENARIO_TRACE_PARSE_FAIL`
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
- `RECOVERY_BUDGET_ACCOUNTING`
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
- `DRIFT_SCAN_RETRY`
- `DRIFT_SCAN_DIFF`
- `DRIFT_SCAN_OK`
- `DRIFT_SCAN_ERROR`
- `SECURITY_PASS_STARTED`
- `SECURITY_PASS_SCOPE`
- `SECURITY_PASS_CLEAN`
- `SECURITY_PASS_BLOCKED`
- `SECURITY_PASS_FIX_ISSUE_CREATED`
- `SECURITY_PASS_FIX_ISSUE_REISSUED`
- `SECURITY_PASS_FIX_ISSUE_SUCCESSOR_ADOPTED`
- `SECURITY_PASS_CYCLE_BUDGET_RESET`
- `SECURITY_PASS_FAILED`
- `SECURITY_PASS_SKIPPED_DISABLED`
- `SECURITY_PASS_JUDGE_SKIPPED`
- `SECURITY_PASS_JUDGE_FAILED`
- `SECURITY_PASS_JUDGE_DECIDED`
- `SECURITY_PASS_WAIVED`
- `SECURITY_PASS_WAIVE_REJECTED`
- `SECURITY_PASS_WAIVED_SUPPRESSED`
- `SECURITY_PASS_ADVISORY_FOLLOWUP_CREATED`

- `SEMBLE_QUERY`
- `SEMBLE_FALLBACK`
- `SERENA_QUERY`
- `SERENA_FALLBACK`
- `SERENA_PROBE`
- `TASK_STATE_UNBLOCK`
- `TASK_STATE_WRITE_FAIL`
- `EVENTS_EMIT`
- `EVENTS_EMIT_FAIL`
- `NAG_REMINDER_LOAD_FAIL`
- `TRANSCRIPT_ARCHIVE_FAIL`
- `IDENTITY_REINJECT_PARSE_FAIL`
- `drift-audit:`
- `CHECK_TRIAGE`
- `WORKTREE_REGISTER`
- `WORKTREE_DEREGISTER`
- `WORKTREE_GC`
- `WORKTREE_REGISTRY_REBUILD`
- `WORKTREE_REGISTER_INVALID_NAME`
- `WORKTREE_REGISTER_FAIL`
- `WORKTREE_DEREGISTER_FAIL`
- `opencode_agent_failure`

When `EVENTS_JSONL_ENABLED=true`, `scripts/emit_event.sh` and
`scripts/emit_event.py` append a fail-open JSONL mirror to
`.events/run-<run_id>.jsonl` after the original stable text line (or
`AI_PHASE_FAILURE_V1` comment marker) emits. The legacy text stream remains
authoritative for current grep-based tooling, and helper-internal
`EVENTS_EMIT*` diagnostics are not mirrored recursively. Phase D currently
emits only `EVENTS_EMIT_FAIL` on helper-write problems; `EVENTS_EMIT` is a
reserved additive success-path prefix for future use.

When `UNATTENDED_TRANSCRIPT_ARCHIVE_ENABLED=true`,
`scripts/transcript_archive.sh` writes a fail-open JSON envelope under
`.transcripts/<sanitized-run_id>-<sanitized-phase>-<ts>.json` from already-captured success-path
output files. Archive helper problems never fail the caller; the helper emits
only `TRANSCRIPT_ARCHIVE_FAIL` on mkdir/read/write/JSON-encode failures.

When wrapper-level nag reminders are enabled,
`scripts/nag_reminder.sh` loads phase-specific reminder text from
`prompts/_nag_reminders.txt`. Reminder-asset lookup problems fail open: the
caller continues without injection and the helper emits only
`NAG_REMINDER_LOAD_FAIL` when the prompt fragment is missing, unreadable, or
missing the requested phase key.

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
LOG_PREFIX.name=WORKFLOW_SCENARIO_TRACE_WRITTEN
LOG_PREFIX.name=WORKFLOW_SCENARIO_TRACE_PARSE_FAIL
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
LOG_PREFIX.name=RECOVERY_BUDGET_ACCOUNTING
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
LOG_PREFIX.name=DRIFT_SCAN_RETRY
LOG_PREFIX.name=DRIFT_SCAN_DIFF
LOG_PREFIX.name=DRIFT_SCAN_OK
LOG_PREFIX.name=DRIFT_SCAN_ERROR
LOG_PREFIX.name=SECURITY_PASS_STARTED
LOG_PREFIX.name=SECURITY_PASS_SCOPE
LOG_PREFIX.name=SECURITY_PASS_CLEAN
LOG_PREFIX.name=SECURITY_PASS_BLOCKED
LOG_PREFIX.name=SECURITY_PASS_FIX_ISSUE_CREATED
LOG_PREFIX.name=SECURITY_PASS_FIX_ISSUE_REISSUED
LOG_PREFIX.name=SECURITY_PASS_FIX_ISSUE_SUCCESSOR_ADOPTED
LOG_PREFIX.name=SECURITY_PASS_CYCLE_BUDGET_RESET
LOG_PREFIX.name=SECURITY_PASS_FAILED
LOG_PREFIX.name=SECURITY_PASS_SKIPPED_DISABLED
LOG_PREFIX.name=SECURITY_PASS_JUDGE_SKIPPED
LOG_PREFIX.name=SECURITY_PASS_JUDGE_FAILED
LOG_PREFIX.name=SECURITY_PASS_JUDGE_DECIDED
LOG_PREFIX.name=SECURITY_PASS_WAIVED
LOG_PREFIX.name=SECURITY_PASS_WAIVE_REJECTED
LOG_PREFIX.name=SECURITY_PASS_WAIVED_SUPPRESSED
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_FOLLOWUP_CREATED
LOG_PREFIX.name=SEMBLE_QUERY
LOG_PREFIX.name=SEMBLE_FALLBACK
LOG_PREFIX.name=SERENA_QUERY
LOG_PREFIX.name=SERENA_FALLBACK
LOG_PREFIX.name=SERENA_PROBE
LOG_PREFIX.name=TASK_STATE_UNBLOCK
LOG_PREFIX.name=TASK_STATE_WRITE_FAIL
LOG_PREFIX.name=EVENTS_EMIT
LOG_PREFIX.name=EVENTS_EMIT_FAIL
LOG_PREFIX.name=NAG_REMINDER_LOAD_FAIL
LOG_PREFIX.name=TRANSCRIPT_ARCHIVE_FAIL
LOG_PREFIX.name=IDENTITY_REINJECT_PARSE_FAIL
LOG_PREFIX.name=drift-audit:
LOG_PREFIX.name=CHECK_TRIAGE
LOG_PREFIX.name=WORKTREE_REGISTER
LOG_PREFIX.name=WORKTREE_DEREGISTER
LOG_PREFIX.name=WORKTREE_GC
LOG_PREFIX.name=WORKTREE_REGISTRY_REBUILD
LOG_PREFIX.name=WORKTREE_REGISTER_INVALID_NAME
LOG_PREFIX.name=WORKTREE_REGISTER_FAIL
LOG_PREFIX.name=WORKTREE_DEREGISTER_FAIL
LOG_PREFIX.name=opencode_agent_failure

---

## Label-repair contradiction policy (current branch)

The active poller loop uses `reconcile_managed_issue_labels` for current-wave
managed issues and logs `LABEL_REPAIR*` diagnostics. The richer
contradiction-evidence helpers in `scripts/orchestrate_lib.py`
(`parse_phase_failure_markers`, `choose_most_advanced_conclusive_evidence`,
`resolve_label_repair_evidence`) are contract/reserved and not yet wired
into poller reconciliation.

---

## DigitalOcean resources

Registry of DigitalOcean resource IDs relevant to this repo, per CLAUDE.md
§22.C. Interactive Claude Code sessions read IDs from this table instead of
asking the user; when a needed ID is missing, the session asks once and then
records it here in the same PR/commit. Consumer repos carry the same section
in their root `AGENTS.md`.

| Resource | Type | ID | Notes |
|---|---|---|---|

_None recorded yet — this repo is workflow tooling and currently has no
DigitalOcean-hosted app or database of its own._

## Cloudflare resources

Registry of Cloudflare identifiers (Worker names, zone IDs, routes, KV/R2/D1
namespace IDs) relevant to this repo, per CLAUDE.md §24.F. Interactive Claude
Code sessions read identifiers from this table instead of asking the user;
when a needed identifier is missing, the session looks it up via a §24.B read
or asks once, then records it here in the same PR/commit. The `Credential`
column names which session env var (`FUNTOKEN_IO_CF` for funtoken.io;
`FT_GAMES_CF` for ft.games and 5m.fun) the resource belongs to. Consumer
repos carry the same section in their root `AGENTS.md`.

| Resource | Type | ID / name | Credential | Notes |
|---|---|---|---|---|

_None recorded yet — this repo is workflow tooling and serves none of the
covered sites (funtoken.io, ft.games, 5m.fun) itself._

## Reference

Operator runbooks (env var reference, autofix retrigger/dedup internals,
orchestrator integration-sync auto-heal, validation self-healing, workflow
log analysis pipeline, semantic cache scope, wrapper pin policy) live in
`./probably_unnecessary_but_read_if_stuck.md`. Read it only when needed —
it is intentionally large.

`CHANGELOG.md` is never edited directly. Write one fragment per PR at
`changelog.d/<issue-or-pr>-<slug>.md`; `scripts/assemble_changelog.py` folds
fragments into `CHANGELOG.md` and deletes them, at release time here
(`mark-stable.yml`, `test-and-mark-stable.yml`) and on the existing sync in
consumer repos (`update_workflows.yml`). Because two PRs never touch the same
path, concurrent PRs cannot conflict on the changelog. `CLAUDE.md` §20 is the
authoritative, self-contained rule — when an entry is required, the fragment
format, and the entry structure and voice rules — and it travels to every
consumer repo verbatim through the root `CLAUDE.md` sync.
`docs/changelog-style.md` is the longer-form contributor-facing companion and
lives in this repo only; consumer repos do not receive it, so §20 must never
depend on it.

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
- Additive Phase M note: `prompts/review-consolidator.txt` now appends an eighth `DOCS COVERAGE (DIATAXIS)` lens after those original seven. The first seven lens names and order stay byte-for-byte stable; the new lens is advisory-only (`SEVERITY: low`, normally `CLASSIFICATION: nice-to-have`), is grounded in reviewer evidence plus touched files for user-visible changes, and names only still-missing `Reference` / `How-to` / `Tutorial` / `Explanation` updates (or `Docs coverage: complete` when already covered).
- Reviewer prompts now carry explicit anti-rules in both `prompts/review-reviewer-checklist.txt` (`WHAT NOT TO FLAG` under each lens) and the shared `COMMON ANTI-RULES` block rendered by `scripts/review_run_reviewers.sh`.
- `scripts/review_run_reviewers.sh` also carries an additive, default-off Phase I `lite | standard | full` review-tier resolver. `lite` requires the existing doc-only path set plus `REVIEW_TIER_LITE_MAX_LOC`; `standard` requires one allowed top-level directory (`scripts/`, `prompts/`, `.github/workflows/`, or `tests/`) plus `REVIEW_TIER_STANDARD_MAX_LOC`; `full` is the force-review and fail-open tier. When enabled, `lite` uses one configured reviewer slug, `standard` uses a configured reviewer subset, `full` keeps the full live roster, `AUTOFIX_SKIP_*` fast paths stay authoritative, `[force-review]` / `force-review` still force full review, and `lite` reuses `REVIEW_CONSOLIDATOR_ENABLED=0` to skip the consolidator.
- `scripts/review_run_reviewers.sh` can classify a PR into `trivial | lite | full` reviewer tiers from reviewer-visible diff LOC/file counts, with `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX` forcing `full` on sensitive paths. Default tier fan-out follows the live `REVIEWER_MODELS` order from `.github/workflows/review_autofix.yml`: trivial = first reviewer, lite = first two reviewers, full = the complete configured set.
- `scripts/review_filter_uninteresting_files.sh` strips low-signal lock/generated/minified paths before reviewer fan-out and emits `REVIEWER_FILTER_SKIP: <path> <reason>` for each skipped file. Default exemptions remain `db/contracts/**`, `**/migrations/**`, and `**/migrate/**`.
- `.github/workflows/review_autofix.yml` now runs a fail-open local slop-scan preflight (gated by `SLOP_SCAN_ENABLED`, default `true`) on PR-changed `scripts/*.py`, `scripts/*.sh`, and `validation/**/*.sh` Python heredocs. It writes `.ai/slop_scan/findings.json`, feeds that JSON to reviewer and consolidator prompts as advisory untrusted context, and removes the runtime artifact before commit-producing steps so it cannot leak into staged changes.
- Consumer-repo review commits snapshot untracked paths before the editor runs in `PRE_EDITOR_UNTRACKED_FILE`. `scripts/review_commit_changes.sh` removes paths that were already untracked plus pipeline-owned artifacts, records removals in `REVIEW_REMOVED_NEW_FILES_FILE`, and preserves other editor-created files for the existing staging and write-guard path; a missing snapshot retains the legacy delete-all fallback.
- `scripts/review_agents_md_materiality.sh` is deterministic-path-glob v1: it writes a JSON result payload plus a non-blocking PR comment headed `## AI Materiality Advisory` when materiality is `high` or `medium` and root `agents.md` is unchanged. `AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED` is reserved only; enabling it still does not trigger a model call in the current shipped script.
- When `REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED=true`, `scripts/review_consolidate.sh` feeds that helper JSON into the consolidator prompt as advisory untrusted context. This is the Lens 7 companion to the separate advisory comment path controlled by `AGENTS_MD_MATERIALITY_ENABLED`. Lens 7 (`NAMING / BACKWARD COMPATIBILITY`) may then emit a default-`high` `AGENTS.md materiality` finding when operator-visible structural changes leave root `agents.md` unchanged, but downgrades or omits it when equivalent touched docs already cover the behavior.
- `REVIEW_LEDGER_REREVIEW_ENABLED` gates consolidator-side suppression of repeated `accepted-residual` / `won't-fix` findings from the existing review ledger and the review-blocked judge's ledger-fed prior-round decision input. `scripts/review_rb_judge.sh` renders that `=== BEGIN PRIOR ROUND DECISIONS ===` block via `render_review_rb_prior_round_decisions_file`, and `prompts/mode-judge-review-blocked.txt` treats it as advisory history rather than fresh reviewer evidence.
- `REVIEWER_CIRCUIT_BREAKER_ENABLED` persists reviewer health under `.ai/review_runtime/pr-<PR>/reviewer_health_state.json`. Retryable reviewer failures first retry with cheaper reasoning, then consult `scripts/reviewer_failback_chains.json`; unmapped reviewers fail open via `REVIEWER_FAILBACK_UNMAPPED`. The live-roster mapping file covers `deepseek/deepseek-v4-pro -> deepseek/deepseek-v3.2`, `minimax/minimax-m3 -> minimax/minimax-m2.5`, `moonshotai/kimi-k3 -> moonshotai/kimi-k2.7-code`, `qwen/qwen3.7-plus -> qwen/qwen3.6-plus`, and `x-ai/grok-4.6 -> x-ai/grok-4.20`; it also retains non-roster override mappings `qwen/qwen3.6-plus -> qwen/qwen3-coder-plus` and `x-ai/grok-4.20 -> x-ai/grok-4.1-fast`. `mistralai/mistral-small-2603` remains intentionally unmapped until the catalog ships a same-family alternate.
- `scripts/cost_audit.py` now parses additive review telemetry fields `cache_hit_rate`, `wall_clock_p50_ms`, `wall_clock_p99_ms`, `break_glass_count`, and `context_budget_warn_count`. `CONTEXT_BUDGET_WARN` is emitted pre-flight from review / consolidator / judge paths when a prompt exceeds the configured per-model context threshold.
- `scripts/codex_heartbeat.sh` wraps long-running `codex exec` calls in reviewer, consolidator, review-blocked judge, conflict-resolver, and validate/self-heal paths, emitting `CODEX_HEARTBEAT: phase=<phase> elapsed_secs=<n>` during silent periods.
- `REVIEW_APPROVAL_RUBRIC_ENABLED` lets the review-blocked judge emit logical `review_state` values (`APPROVE`, `APPROVE_WITH_COMMENTS`, `COMMENT`, `REQUEST_CHANGES`) that `scripts/post_review_comment.sh --review-state` maps to outbound PR reviews. With `REVIEW_BREAK_GLASS_ENABLED`, a human comment anchored as `@codex break-glass` downgrades only the outbound `REQUEST_CHANGES` event to comment-only and logs `BREAK_GLASS`, while preserving the judge's written review body.
- The `CI / lint` job shards the 307-test post-fast-fail subset of `tests/test_orchestrate_poll_process.py` across `CI_POLL_TEST_SHARDS` workers (default 4). That module is CI's critical path: most tests in the subset spawn the real poller as a bash subprocess inside a throwaway sandbox, costing seconds each rather than milliseconds, and run sequentially it took roughly 35 minutes on a 4-core box. That alone overran the job's `timeout-minutes: 30`, so every CI run — on `main` as well as on pull requests — was cancelled mid-suite and the repo had no completing full-test gate. Each test allocates its own tempdir sandbox in `_make_poller_sandbox`, so the module shards with no shared state; the split is `NR % total == n`, a true partition verified by `tests/test_ci_poll_test_sharding.py`. A failing shard fails the step, and a shard whose exit code was never recorded counts as failed rather than passing silently. The job budget is 45 minutes, leaving headroom over the sharded runtime for a cold runner. The release gates carry the same sharded step: the `validate-scripts` job in `mark-stable.yml` and `test-and-mark-stable.yml` shards the full module (no fast-fail subset) under the same `CI_POLL_TEST_SHARDS` var and a 45-minute budget, after release v1.27.0 (run 33073743283) was lost to the serial module overrunning that job's previous 30-minute cap — the cancelled job skipped `validate`/`release`. `tests/test_ci_poll_test_sharding.py` pins the ported step's partition expression, failure handling, and budget in both workflows.
- `scripts/review_resolve_review_threads.sh` closes the loop on PR review comments. The pipeline has always *read* them — `scripts/review_collect_pr_metadata.sh` fetches `issues/<pr>/comments` and `pulls/<pr>/comments` into `PR_ALL_COMMENTS_CONTEXT_FILE`, which both `review_run_reviewers.sh` and `review_apply_fixes.sh` inline, and the editor must audit each one under `PR comment audit:` — but nothing marked the thread resolved, so a fixed comment looked identical to an unread one. The `Resolve addressed PR review threads` step in `review_autofix.yml` now runs after the editor summary and its persisted-change/no-op safety checks, then resolves each validated audited thread; an `applied` disposition additionally requires a productive commit. Mapping is keyed on the comment **id** carried by the audited `entry[N]`, resolved through `PR_ALL_COMMENTS_CONTEXT_FILE`, never on a path/line pair: an entry the editor never listed, an index whose audited path disagrees with the real comment's path, a non-`review_comment` kind, and an already-resolved thread are all skipped. That is what keeps two contradictory comments at one file:line from resolving each other. `ignored` entries are resolved too, but only after the editor's stated reason is posted as a thread reply, so the reviewer sees the disagreement and can reopen. Thread lookup is one paginated GraphQL query per run (§15); GraphQL is used because REST exposes no resolve-review-thread endpoint, and the §21.D/§23.D REST preference addresses the Claude Code Web proxy in interactive sessions, not this Actions-side caller. Every failure path warns and exits 0.
- `.github/workflows/review_autofix_sweep.yml` skips a PR whose head ref already has a `queued` or `in_progress` review run, so a 30-minute tick cannot stomp a synchronize-fired run mid-edit. That guard now distinguishes the two states. `in_progress` suppresses indefinitely — the codex-agent job legitimately runs over an hour. `queued` suppresses only until `SWEEP_STALE_QUEUED_MINUTES` (default 120, above the ~94-minute longest observed legitimate concurrency wait), because GitHub can wedge a run in `queued` with zero jobs and then reject both `cancel` (409 `Cannot cancel a workflow run that has not been queued yet`) and `rerun` (403 `This workflow is already running`). With no cutoff such a run suppressed the sweep forever, and the sweep is the PR's only recovery path, so the guard deadlocked the mechanism it protects — PR #3841 sat unreviewed for 11+ hours behind run `32984498460`. Discounted runs are logged as `AUTOFIX_SWEEP_STALE_QUEUED`, never dropped silently; a run with a missing or unparseable `created_at` still counts as active, and `SWEEP_STALE_QUEUED_MINUTES=0` restores the previous behaviour exactly. The guard also counts `pending` runs — a duplicate dispatch held back by `review_autofix.yml`'s `cancel-in-progress: false` concurrency group reports `pending`, not `queued`, and the same is true for the poller's `_has_active_autofix_run` guard in `scripts/orchestrate_poll_process.sh`. `pending` suppresses indefinitely, like `in_progress` (it is bounded by the running peer's 240-minute job timeout), and is never subject to the stale-queued cutoff. To keep every review run visible to these head-branch-keyed lookups, the sweep and `forward-merge-stable-to-main.yml`'s fallback-PR review dispatch both pass `--ref <head branch>` for same-repo PRs (the sweep falls back to the default branch for fork PRs, whose head ref does not exist in the repo). Before this, the PR #3895 incident (2026-08-29) accumulated 10 duplicate dispatches and 6+ duplicate Telegram conflict warnings in ~95 minutes while the one real resolver — dispatched on `main`'s ref, invisible to both guards — ran to success.
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
| `REVIEW_CONSOLIDATOR_MODEL` | `openai/gpt-5.6-sol` | Default consolidator model in `review_autofix.yml`. |
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
| `CI_POLL_TEST_SHARDS` | `4` | Parallel shards for the orchestrate-poll module in `CI / lint` and in the release gates' `validate-scripts` job. `1` is sequential; invalid values warn and fall back to `1`. |
| `CONFLICT_MANIFEST_UNION_ENABLED` | `true` | Deterministically resolve two-sided `.ai/.workspace_source_manifest.txt` content conflicts before the model resolver; manifest-only conflicts are committed as `[ai-merge-resolve]` and skip the model. Integration-sync branches and delete/modify conflicts remain model-resolved. |
| `REVIEW_RESOLVE_THREADS_ENABLED` | `true` | Resolve PR review threads the editor audited in its `PR comment audit:` section. Keyed on comment id, so two comments at one path cannot resolve each other; `ignored` entries get the editor's reason as a reply before resolving. |
| `REVIEW_RESOLVE_THREADS_MAX` | `50` | Per-run cap on resolved review threads; anything above it is warned about and left open. |
| `SWEEP_STALE_QUEUED_MINUTES` | `120` | Age past which a still-`queued` review run stops suppressing a sweep dispatch (wedged-run recovery). `in_progress` runs are never discounted; `0` disables the cutoff. |
| `REVIEW_TIER_RESOLVER_ENABLED` | `false` | Enable the additive Phase I `lite \| standard \| full` review-tier resolver. While `false`, existing reviewer routing is unchanged. |
| `REVIEW_TIER_LITE_MAX_LOC` | `50` | Maximum total diff LOC for `lite` review-tier resolution. `lite` also requires the existing doc-only path set. |
| `REVIEW_TIER_LITE_REVIEWER_SLUG` | `qwen/qwen3.7-plus` | Reviewer slug used for the `lite` review tier when the Phase I resolver is enabled. Unknown or unavailable slugs fail open to `full`. |
| `REVIEW_TIER_STANDARD_MAX_LOC` | `200` | Maximum total diff LOC for `standard` review-tier resolution. `standard` also requires changes confined to one allowed top-level directory. |
| `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` | `minimax/minimax-m3,deepseek/deepseek-v4-pro,x-ai/grok-4.6` | Comma-separated reviewer subset for the `standard` review tier when the Phase I resolver is enabled. Unknown or unavailable slugs fail open to `full`. |
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
| `AGENTS_MD_MATERIALITY_ENABLED` | `1` | Post the deterministic, non-blocking `AGENTS.md` materiality advisory comment when a material change omits an `agents.md` update (on by default; set `0` to disable). |
| `AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED` | `0` | Reserved only; deterministic v1 still makes no materiality model call when this flag is on. |
| `AGENTS_MD_MATERIALITY_MODEL` | `openai/gpt-5.6-luna` | Reserved future materiality fallback model slug. |
| `AGENTS_MD_MATERIALITY_REASONING` | `medium` | Reserved future materiality fallback reasoning effort. |
| `CONTEXT_BUDGET_WARN_RATIO` | `0.7` | Per-model context-window ratio above which review-surface prompt builders emit `CONTEXT_BUDGET_WARN`. |
| `MAX_PROMPT_TOKENS_FOR_PHASE` | `(empty)` | Absolute prompt-token override that takes precedence over `CONTEXT_BUDGET_WARN_RATIO`; phase-specific `MAX_PROMPT_TOKENS_FOR_<PHASE>` overrides remain supported. |
| `CODEX_HEARTBEAT_ENABLED` | `1` | Enable the `codex_heartbeat.sh` wrapper on long-running review / validate Codex calls. |
| `CODEX_HEARTBEAT_INTERVAL_SECS` | `30` | Silence interval (seconds) between emitted `CODEX_HEARTBEAT` lines. |
| `REVIEW_DIATAXIS_LENS_ENABLED` | `true` | Documentation-only contract row for the advisory `DOCS COVERAGE (DIATAXIS)` consolidator lens. Current branch behavior is prompt-defined only (no separate workflow toggle yet): keep it `low` severity and name only still-missing `Reference` / `How-to` / `Tutorial` / `Explanation` updates. |
| `REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED` | `true` | Enable the consolidator-side companion `AGENTS.md` materiality finding. Unlike `AGENTS_MD_MATERIALITY_ENABLED`, which controls the separate advisory comment helper, this flag only controls whether `review_consolidate.sh` passes the helper JSON into Lens 7 (`NAMING / BACKWARD COMPATIBILITY`). |
| `ENABLE_SECURITY_PASS` | `true` | Enable the scheduled poller's mandatory current-integration-head security gate before validation or finalization. Set to `false` for the immediate operator kill switch and legacy completion behavior. |
| `MAX_SECURITY_PASS_CYCLES` | `5` | Maximum completed consolidated security-fix cycles before persistent findings terminalize as `ai:security-pass-failed`. Resets to `0` when an advancing integration head invalidates a recorded clean pass. Re-audits after a merged fix are delta audits, so the budget bounds persisting findings rather than fresh samples of unchanged code. |
| `MAX_SECURITY_PASS_FIX_REISSUES` | `2` | Maximum re-issues of one `ai:implementation-failed` consolidated security-fix issue per fix cycle before the pass terminalizes as `ai:security-pass-failed`. |
| `SECURITY_PASS_CONFIDENCE_GATE` | `8` | Minimum 1-10 confidence score for findings that block the project security pass. |

## Integration-sync verifier + bootstrap contract

- `scripts/verify_integration_fingerprints.py` supports `--baseline-fingerprints-state <out>` / `--compare-against-baseline <in>` alongside `--ref`; capture mode records ref-accurate `head_sha` metadata, compare mode emits `PRE_EXISTING_FINGERPRINT_DRIFT_V1` markers for pre-existing drift that should not block the resolver commit, and the verifier-side false-positive defenses emit `FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1` (capture-side multi-occurrence partial removal), `FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1` (a `must_contain` line modified after capture by a non-`[ai-merge-resolve]` commit), and `FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1` (a `must_not_contain` line re-added after capture by a non-`[ai-merge-resolve]` commit — e.g. a back-merge of the default branch keeping its still-present copy) when the ref-mode wave-dispatch gate suppresses a non-resolver false positive. The two post-capture defenses share one direction-agnostic pickaxe primitive and both fail closed in working-tree mode, so the resolver's own pre-commit self-check stays strict and still cannot silently revert merged intent.
- `.github/workflows/review_autofix.yml` stages `verify_integration_fingerprints.py`, `review_conflict_prepare.sh`, `review_conflict_resolve.sh`, `render_prompt.py`, `opencode_helpers.sh`, and `write_opencode_config.sh` through `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` (main snapshot first, branch fallback). `render_prompt.py` is main-primary so the newest renderer (and any bundled contract/reference assets) reaches an in-flight PR whose branch predates the fix. The reviewer/editor prompt bodies embed arbitrary PR-diff + comment text that can carry literal `{{...}}` / `{%...%}` tokens, so their post-embed render calls now pass `--skip-syntax-validation` (opt-in via `RENDER_PROMPT_SKIP_SYNTAX_VALIDATION=1` in `render_prompt.sh`) — the strict `validate_supported_template_syntax` gate is skipped for those already-assembled bodies while placeholder substitution still runs. This removes the false-positive class at the source (an embedded diff token no longer hard-fails the whole reviewer/editor step, so a docs/diff carrying template syntax — run 28936678508 — or the earlier lone-`${{` case, PR #3592 / #3593 / run 28888093412, cannot wedge the review). The gate stays strict for every static template render (e.g. the editor continuation prompt `prompts/mode-review-apply-fixes-continuation.txt`), so genuine prompt-authoring errors are still caught. `render_prompt.py` stays fail-open — when the backend is absent from both refs, bootstrap preserves the ref's bundled bash renderer. `OPTIONAL_BOOTSTRAP_SCRIPTS` is reserved for genuinely optional helpers only.
- `scripts/review_merge_train.sh` is staged through `REQUIRED_BOOTSTRAP_SCRIPTS` for `review_autofix.yml` and copied best-effort (with `label_helpers.sh`) next to `gh_helpers.sh` by `orchestrate_poll.yml` and `cancel_on_pr_close.yml`, which do not run the full support staging. Both callers treat a missing copy as "skip this tick" so an older `SCRIPT_REF` keeps polling; its own API budget is documented in the script header (CLAUDE.md §15).
- `scripts/review_conflict_resolve.sh` persists one `AUTOFIX_RESOLVER_RETRY_STATE_V1` PR-body block per final PR/head SHA, keyed by normalized fingerprint failure signature. `RESOLVER_ESCAPE_THRESHOLD_N` is the per-tier same-head, same-signature step size: multiples advance `strict` → `ratio` → `count_only` → `warn_only`, emit `FINGERPRINT_TIER_DOWNGRADED_V1`, and after the next multiple the script labels the **final PR issue** `ai:resolver-escalated` and records `escalated_at` for poller-side suppression / branch-rebuild gating.
- `scripts/verify_integration_fingerprints.py` uses `FINGERPRINT_QUARANTINE_RUNS_M` to move stable unchanged drift into ai-memory quarantine and emits `FINGERPRINT_QUARANTINED_V1` markers when the skip path activates. `.github/workflows/drift-audit.yml` (cron `0 3 * * *`, gated by `DRIFT_AUDIT_ENABLED`) scans `PRE_EXISTING_FINGERPRINT_DRIFT_V1` / `FINGERPRINT_QUARANTINED_V1` markers and maintains tracker issues for persistent clusters. The audit skips any cluster whose fingerprint path is absent from the repository checkout, so markers echoed from test fixtures or PR diffs (synthetic paths such as `scripts/example.py`) do not open tracker issues. Completed runs concluded `cancelled` / `skipped` routinely upload no logs (concurrency-superseded review runs); a failed log fetch for them is classified `unscannable` rather than missing, keeping coverage `full` so the per-run Telegram summary stays at DEBUG instead of firing a daily partial-coverage WARNING; their logs are still scanned when present. Every enabled run posts a Telegram run summary (`tg_send_msg`, gated by `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID`) linking to the run and writes a GitHub Actions job summary.
- `.github/workflows/security-audit.yml` (weekly `0 8 * * 0` plus `workflow_dispatch` plus `workflow_call`, gated by `SECURITY_AUDIT_ENABLED`, default `true`) is a default-branch maintenance audit that runs on the source repo and, via the synced `workflow-templates/ai-security-audit.yml` wrapper, on every consumer repo against its own default branch (consumer runs stage this repo's `scripts/` + `prompts/` from a `@stable` support checkout into `SECURITY_AUDIT_SUPPORT_DIR` and need `OPENROUTER_API_KEY`, optionally `GH_PAT`). It runs `scripts/security_audit.sh` with `prompts/mode-security-audit.txt`, appends dated findings sections to the stable `AI Security Audit Tracker` issue (`ai:security-audit`, marker `<!-- ai:security-audit-tracker:v1 -->`), and opens up to 3 weekly `ai:security` follow-up issues after confidence-gate + false-positive-exclusion filtering. Each completed run records the audited HEAD on the tracker body (marker `<!-- ai:security-audit-last-sha:… -->`); the next run skips entirely when HEAD is unchanged (`SECURITY_AUDIT_SKIP_IF_UNCHANGED=true`, log-only skip) and otherwise diff-scopes the audit to the commits since that SHA (`SECURITY_AUDIT_INCREMENTAL=true`; the post-filter drops findings citing unchanged files as `suppressed_out_of_scope`; first runs, history rewrites, and >200-file diffs fall back to the full scope). `.github/workflows/internal-clarify.yml` skips `ai:security-audit` issues so tracker bookkeeping never recurses into the normal clarify/plan pipeline.
- `scripts/security_audit.sh` exposes `SECURITY_AUDIT_OUTPUT_MODE=findings-json` for the default-on orchestrator project security pass. It accepts an optional project-spec file, supports a fail-closed explicit `SECURITY_AUDIT_DIFF_BASE`/`SECURITY_AUDIT_DIFF_HEAD` range, optionally narrows that range with `SECURITY_AUDIT_DIFF_SINCE` (only range files changed since that commit stay in scope; fails closed on an unresolvable or non-ancestor commit) and re-verifies `SECURITY_AUDIT_PRIOR_FINDINGS` (a JSON array of earlier findings whose files stay in scope and which the prompt asks the model to re-emit under the same ID if they persist, alongside every remaining instance of the same class; fails closed on malformed input), applies the existing validation/exclusion/confidence/scope filters, and atomically publishes `security_audit_findings.v1` to `SECURITY_AUDIT_FINDINGS_OUT`. This mode performs no GitHub tracker, label, follow-up, last-SHA, or notification side effects; the default `issues` path remains the production weekly mode.
- `.github/workflows/workflow-log-analysis.yml` now also has a source-repo-only weekly retro path (cron `0 9 * * 1`, gated by `WORKFLOW_RETRO_ENABLED`, default `true`). `WORKFLOW_RETRO_CRON` defaults to the same cron string and must stay in sync with the trigger because GitHub does not interpolate vars into `on.schedule`. The workflow builds retro context with `scripts/workflow_retro.py`, renders the narrative through `prompts/mode-workflow-analysis.txt` in retro mode using `WORKFLOW_RETRO_MODEL` / `WORKFLOW_RETRO_REASONING` (defaults `openai/gpt-5.6-luna` / `medium`), and posts into the stable `AI Workflow Weekly Retro` tracker issue (`ai:retro`, marker `<!-- ai:retro-tracker:v1 -->`). Zero-activity windows (no workflow runs and no merged PRs; `has_activity: false` in the `workflow_retro.v1` JSON) skip the LLM pass and the tracker comment when `WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY=true` (default), leaving only a `WORKFLOW_RETRO_SKIP_V1:` line in the run log and no Telegram alert. After the source-repo retro, the `Consumer retro fan-out` step (gated by `WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED`, default `true`) runs `scripts/workflow_retro_fanout.sh`: for each repo in `.github/ai/consumer_repos.json` (source repo excluded) it builds a per-repo retro from the same collect-logs artifact, honors the consumer's own `WORKFLOW_RETRO_ENABLED` repo var (one fail-open `gh api` GET per consumer per week), applies the same no-activity skip, and upserts the week-marked comment on that consumer's `AI Workflow Weekly Retro` tracker via `GH_PAT` (§14 repo scope). Per-repo outcomes are logged as `WORKFLOW_RETRO_FANOUT_V1: repo=… status=posted|refreshed|up_to_date|skipped_no_activity|skipped_disabled|failed`; individual failures fail open and the step errors only when every attempted consumer fails. Both `.github/workflows/internal-clarify.yml` (source repo) and the consumer-facing gate in `.github/workflows/clarify.yml` skip `ai:retro` / `ai:security-audit` issues so tracker upkeep never recurses into the clarify/plan pipeline.
- `scripts/orchestrate_poll_process.sh` gates last-resort `orchestrator/project-*` branch rebuilds behind `BRANCH_REBUILD_ENABLED`, `BRANCH_REBUILD_THRESHOLD_HOURS`, and `BRANCH_REBUILD_COOLDOWN_HOURS`. Audit snapshots are persisted as `BranchRebuildAuditV1` in `ai-memory/schemas/branch_rebuild_audit.v1.json` (this shipped artifact supersedes the old plan placeholder name `BRANCH_REBUILD_AUDIT_V1`; there is no literal runtime marker with that string).

## Operational lessons learned (categorised)

**General / Tooling**
- Treat the `openai/codex#11151` no-edit regression as closed only with function-style patch tooling; keep `apply_patch_tool_type = "function"` as the settled baseline. Pointers: `scripts/codex_model_catalog.json`, `scripts/write_codex_config.sh`.
- Keep `low` as the default gpt-5.6-sol verbosity across workflow entrypoints unless a specific phase re-proves the old announce-without-emit failure. Pointers: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate.yml`.

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
- Lessons-learned memory uses the standalone schema `ai-memory/schemas/lessons_learned_record.v1.json`; review-autofix writes issue-scoped records under `ai-memory/tasks/issue-*/lessons_learned/` via `scripts/ai_memory_lib.py::record_lessons_learned`, and plan-mode prompts treat surfaced same-file lessons as soft priors rather than hard requirements.
- Operator-facing memory hygiene lives in `scripts/ai_memory.py`: `review --since <duration>` lists stale task candidates, `prune --record-id <id>` marks task candidates for the existing monthly `compact --prune true` archival path, `search --query <text>` prefers OpenRouter embeddings when `OPENROUTER_API_KEY` is set and otherwise falls back to keyword ranking, and `export --issue <n>` / `--pr <n>` dumps matching memory records as JSON. `prune` is intentionally additive: it writes a `timestamps.prune_marked_at` marker on candidate records instead of introducing a second maintenance channel.

**Validation harness Docker lifecycle**
- Validation containers distinguish `/bin/sh -c` from `/bin/sh -lc`; shell choice is part of harness correctness, not a cosmetic variation. Pointers: `scripts/validation_lint.py`, `prompts/mode-validate-generate.txt`.
- npm/yarn/pnpm wrapper shutdown handling and `mongosh` apt-repo constraints are harness invariants; keep the existing SIGTERM/exit-code and package-source rules intact. Pointers: `scripts/validate_driver.sh`, `prompts/mode-validate-fix-harness.txt`.

## Repo-tree (auto-generated)

Active workflow files (regenerate with `make generate`):

<!-- TREE:START id=workflows -->
```
.github/workflows/audit_consumer_drift.yml
.github/workflows/cancel_on_pr_close.yml
.github/workflows/check_failure_triage.yml
.github/workflows/ci.yml
.github/workflows/clarify.yml
.github/workflows/comprehensive-test-and-release.yml
.github/workflows/drift-audit.yml
.github/workflows/forward-merge-stable-to-main.yml
.github/workflows/implement.yml
.github/workflows/integration-pr-readiness.yml
.github/workflows/internal-cancel-on-pr-close.yml
.github/workflows/internal-check-failure-triage.yml
.github/workflows/internal-clarify.yml
.github/workflows/internal-implement.yml
.github/workflows/internal-issue-pr-status.yml
.github/workflows/internal-memory-maintenance.yml
.github/workflows/internal-orchestrate-clarify-respond.yml
.github/workflows/internal-orchestrate-poll.yml
.github/workflows/internal-orchestrate.yml
.github/workflows/internal-plan.yml
.github/workflows/internal-review.yml
.github/workflows/internal-validate.yml
.github/workflows/issue_pr_status.yml
.github/workflows/lint-plan-archival.yml
.github/workflows/lint-pr-body-auto-close.yml
.github/workflows/mark-stable.yml
.github/workflows/memory_maintenance.yml
.github/workflows/nightly-validation-selftest.yml
.github/workflows/opencode-live-smoke.yml
.github/workflows/orchestrate.yml
.github/workflows/orchestrate_clarify_respond.yml
.github/workflows/orchestrate_poll.yml
.github/workflows/plan.yml
.github/workflows/promote-main-to-stable.yml
.github/workflows/review_autofix.yml
.github/workflows/review_autofix_sweep.yml
.github/workflows/review_rb_judge_dispatch.yml
.github/workflows/security-audit.yml
.github/workflows/sync_ai_labels.yml
.github/workflows/test-and-mark-stable.yml
.github/workflows/update_workflows.yml
.github/workflows/validate.yml
.github/workflows/validation-improvements-intake.yml
.github/workflows/validation-refresh.yml
.github/workflows/workflow-log-analysis.yml
.github/workflows/workspace-cache-maintenance.yml
```
<!-- TREE:END id=workflows -->

Consumer-facing workflow templates (regenerate with `make generate`):

<!-- TREE:START id=workflow_templates -->
```
workflow-templates/ai-cancel-on-pr-close.yml
workflow-templates/ai-check-failure-triage.yml
workflow-templates/ai-clarify.yml
workflow-templates/ai-implement.yml
workflow-templates/ai-issue-pr-status.yml
workflow-templates/ai-memory-maintenance.yml
workflow-templates/ai-orchestrate-clarify-respond.yml
workflow-templates/ai-orchestrate-poll.yml
workflow-templates/ai-orchestrate.yml
workflow-templates/ai-plan.yml
workflow-templates/ai-review.yml
workflow-templates/ai-security-audit.yml
workflow-templates/ai-sync-labels.yml
workflow-templates/ai-update-workflows.yml
workflow-templates/ai-validate.yml
workflow-templates/review_rb_judge_dispatch.yml
```
<!-- TREE:END id=workflow_templates -->

## Task-state files (`.tasks/<wave>/<issue>.json`)

- Feature flag: `ORCH_TASK_FILES_ENABLED` (default `false`). When disabled, `scripts/task_state.py` is a no-op and the poller writes only the authoritative chunked GitHub-comment state.
- Schema: each mirrored file is a single issue payload plus `schema_version: "task_state.v1.json"`.
- Layout: wave directory from `waves[].wave`, filename from the stable local wave issue `id` (for example `.tasks/1/issue-1.json`). Existing fields such as `github_issue`, `depends_on`, and `reissue_depends_on` are preserved verbatim inside the mirrored JSON.
- Write path: `scripts/orchestrate_poll_process.sh::post_state_comment()` mirrors every successful authoritative checkpoint into `.tasks/` via atomic tmp-write + rename.
- Unblock path: `scripts/task_state.py::unblock_dependents()` rewrites only the mirrored files, removing a completed issue from `depends_on[]` / `reissue_depends_on[]` and logging `TASK_STATE_UNBLOCK <wave> <completed> <count_unblocked>`.
- Authority: `.tasks/` is mirror-only in Phase C. The chunked `ORCHESTRATOR_STATE_V2` / `STATE_FILE` path remains the sole read source until a future cut-over plan lands.
- Fail-open logging: mirror write and unblock write failures log `TASK_STATE_WRITE_FAIL <issue> <reason>` and do not stop the poll loop.

## Worktree registry (`.worktrees/index.json`)

- Feature flag: `ORCH_WORKTREE_REGISTRY_ENABLED` (default `false`). When disabled, `scripts/worktree_registry.sh` is bypassed and the existing bare `git worktree` lifecycle remains authoritative.
- Schema: the registry root is `{ "schema_version": "worktree_registry.v1.json", "entries": [...] }`.
- Entry shape: each entry records `name`, `path`, `branch`, `task_id`, `created_at`, `owner_phase`, and `owner_run_id`.
- Write path: `scripts/orchestrate_poll_process.sh` registers worktrees only after `git worktree add` succeeds and deregisters them before the matching remove/fallback cleanup path runs.
- GC path: the existing `internal-orchestrate-poll.yml` `*/5` cadence reaches `scripts/worktree_gc.sh` through the reusable `.github/workflows/orchestrate_poll.yml` job, so stale registry/worktree cleanup rides the poller's existing schedule instead of adding a new cron surface.
- Active-run safety: GC first reuses `${RUNTIME_DIR}/state_snapshot_actions_runs.json`, then the cached `scripts/ai_memory.py actions-runs-cache get --repo <owner/repo>` payload, and treats `queued` plus `in_progress` owner runs as live.
- Fail-open logging: invalid names emit `WORKTREE_REGISTER_INVALID_NAME`; registry rebuilds emit `WORKTREE_REGISTRY_REBUILD`; register/deregister I/O failures emit `WORKTREE_REGISTER_FAIL` / `WORKTREE_DEREGISTER_FAIL`; successful lifecycle events emit `WORKTREE_REGISTER`, `WORKTREE_DEREGISTER`, and `WORKTREE_GC`.

## Phase wrapper predicate parity

- Internal `internal-{clarify,plan,implement,orchestrate-clarify-respond}.yml` callers and their `workflow-templates/ai-*.yml` consumer counterparts mirror the complete job-level `if` predicate from the reusable workflow they invoke. Reusable predicates are canonical; `tests/test_phase_wrapper_predicate_contract.py` prevents actor, association, command-marker, issue-kind, and tracker-exclusion drift.
