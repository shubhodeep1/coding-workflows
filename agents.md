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
   clarifier's questions on orchestrator-managed and standalone issues.
   Both clarification workflows run the selected Claude or Codex engine through
   `scripts/clarify_isolated_run.sh`
   in a read-only, network-isolated container with a host-side model broker.
   They stage the helper and Dockerfile from the support ref (main fallback);
   isolation failures never fall back to host Codex. GitHub-side fetching,
   memory, retry, and comment handling remain on the runner.
   Standalone clarify-respond skips semantic-cache lookup and storage (including
   SQLite restore/save) because live referenced PR, branch and run state is
   absent from the cache key. Orchestrator mode keeps the existing cache path.
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
   The `gate` job also runs an identical-failure fingerprint cap: every
   failure comment ends with a `review-autofix-failure:v1` marker (head,
   reason, fingerprint of the failing stage's stderr), and once
   `REVIEW_FAILURE_FINGERPRINT_MAX_IDENTICAL` (default 3) trailing markers on
   the current head share one fingerprint the gate skips the run
   (`skip_reason=fingerprint_cap`) and the `fingerprint-cap-block` job labels
   the linked issues (or the PR) `ai:review-blocked`, posts one
   `review-autofix-failure-cap:v1` comment and sends an
   `identical_failure_cap` heal report (`REVIEW_FAILURE_FINGERPRINT_CAP_ENABLED`;
   force_rb_judge dispatches bypass it). The poller's noop-suspicious
   recovery sweep (`scripts/orchestrate_poll_process.sh`) does not
   re-dispatch a PR whose current head already has a
   `review-autofix-failure-cap:v1` comment by the `GH_PAT` account, because
   the gate would end that run before a new warning could be posted; it logs
   `NOOP_RECOVERY_SKIP_FINGERPRINT_CAP` instead of sending the "retry N/3"
   Telegram WARNING. A push clears the skip, and an unresolvable head SHA or
   token identity keeps the old re-dispatch.
   The review editor's disposable Docker workspace admits `.cjs`, `.mjs`,
   `.cts`, and `.mts` alongside other source extensions for snapshot and
   validated transfer. For Claude engine fixes it also admits only
   `.github/ai/claude_engine.json`, `.claude/hooks/gh_api_write_guard.py`,
   `.claude/hooks/pr_merge_status_guard.py`, and
   `scripts/claude_settings.json.tmpl`. It also admits each
   `.claude/commands/<name>.md` whose `workflow-templates/.claude/commands/<name>.md`
   twin exists both in the host checkout and the verified workflow-support
   checkout (`GITHUB_WORKSPACE/.codex-workflow-src`) when the snapshot is
   taken. A PR-added twin absent from trusted support cannot authorize a new
   command; missing support admits none. The admitted command set is fixed
   for the whole run; later transfers and retries cannot widen it. The editor
   can still repair parity for existing supported commands; other `.github/ai/`
   and `.claude/` files remain excluded from
   snapshot and transfer. An editor write to an excluded file in an
   admitted directory is dropped; a new directory outside the admitted ones
   fails the transfer (`reason=unsafe_directory`) and the editor step with it. Its
   isolation helpers must already exist in the verified workflow support
   commit; a PR's own copies are review data,
   not executable support, so review fails closed until that commit lands.
   PR-backed `claude/*` heads take the normal review path like every other
   PR: the GPT editor, conflict resolver, review-blocked judge and auto-merge
   all run on them. The former Claude-fixer hand-off (the reviewer panel
   handing findings to a claude.ai session) was retired together with
   CLAUDE.md §26 / §28; `claude_fixer_converged_head` is still accepted by
   `review_autofix.yml` for caller compatibility but ignored, and
   `CLAUDE_FIXER_ENABLED` is read but unused until Phase 5c. The
   `claude-fixer-auto-merge` job id is kept but never runs.
   `[claude-intervention]` and `[claude-merge-resolve]` commits on older PR
   heads still end the counted run, like `[judge-fix]` and
   `[ai-merge-resolve]`.
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
14. **workflow failure heal** (`workflow_failure_heal.yml`,
    `internal-workflow-failure-heal.yml`, `workflow-failure-heal-intake.yml`,
    `scripts/workflow_failure_heal_report.sh`,
    `scripts/workflow_failure_heal_intake.sh`, `scripts/workflow_failure_heal.py`,
    `prompts/mode-workflow-failure-heal.txt`) — triggers on `issues: labeled` /
    `pull_request: labeled` with a human-needed escalation label
    (`ai:needs-human`, `ai:check-triage-escalated`, `ai:destructive-blocked`,
    `ai:scope-blocked`, `ai:harness-broken`, `ai:resolver-escalated`,
    `ai:security-pass-failed`) in a consumer or in this repo, and on
    `workflow_run: completed` failures of the five release / promotion
    workflows. The reporter links the failed runs and the wrapper release pin
    and sends a `repository_dispatch` (`workflow-failure-heal`) to this repo;
    the intake fetches the failed job logs, diagnoses against the source at
    that SHA, classifies (`workflow-defect` / `inconclusive` → issue here with
    `Target branch: stable`, or, for a review/autofix failure from a PR in
    this repo, the branch its support scripts came from: `main` or `stable`
    by one compare call each (prefer `stable` when both contain the SHA,
    since its hotfix is forwarded to `main`), falling back to `stable` if the
    resolved support branch disappears; the PR's head branch only when `script_ref` is
    the PR's own head SHA, else `stable` (`target_branch_source=support_ref`);
    `consumer-app-defect` → issue in the consumer;
    `consumer-config` / `transient` → Telegram + comment only;
    `already-fixed` → Telegram + comment only, honoured only when its
    `## Fixed by` section cites a commit that landed after the failing SHA,
    otherwise filed as `inconclusive`; for a
    review/autofix failure from this repo whose crash file the intake can
    attribute, `pr-self-inflicted` → diagnosis comment on the PR, no issue,
    and `base-self-inflicted` → issue targeting the PR's base branch with
    orchestrator lineage lines and `ai:orchestrator-managed` for an
    `orchestrator/project-<N>` base; ownership comes from the report's
    `changed_files` / `crash_file` and the intake's own
    `git diff origin/main origin/<base>`; with no crash file, a run that
    staged the PR head's scripts (`script_ref` = head SHA) of a PR changing
    `scripts/`, `.github/actions/` or `review_autofix.yml` is still `pr`
    ownership (`basis=pipeline_files`), and a token it does not back, or
    `WORKFLOW_HEAL_SELF_INFLICTED_ROUTING_ENABLED=false`, routes as
    `workflow-defect`). The prompt carries the branch progress since the
    failing SHA (one REST compare call + a branch-tip worktree) and the earlier
    heal issues of the same fingerprint / lineage. It de-dupes by fingerprint
    (label `ai:workflow-heal`; the promote cycle's `[cycle:<id>]` run-name
    suffix is ignored, and the error signature comes from the steps'
    `##[error]` output, not the echoed step script; for an `autofix_failure`
    report it comes from the evidence minus the reporter's header lines,
    `error-signature --strip-autofix-header`, led by the
    `AUTOFIX_FAILURE_FIRST_ERROR` line the reporter adds; an
    `identical_failure_cap` report uses its payload `failure_fingerprint`), de-dupes an
    `autofix_failure` report also by its pull request's `source=` marker (that
    PR's closed heal issues, and the heal issue its `ai/issue-<N>` head branch
    fixes, continue the lineage), caps the lineage at
    `WORKFLOW_HEAL_MAX_LINEAGE_DEPTH` (escalates with
    `ai:workflow-heal-escalated` + Telegram), and bounds the volume with
    `WORKFLOW_HEAL_MAX_OPEN_ISSUES` / `WORKFLOW_HEAL_MAX_ISSUES_PER_DAY`. A
    third reporter lives in the failure path of `review_autofix.yml`
    (`scripts/workflow_failure_heal_autofix_report.sh`, payload kind
    `autofix_failure`): it reports a failed review/autofix run on a pull
    request once `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` (default 2) runs in a
    row failed on that PR, counted from the workflow's own failure comments,
    so the stall poller's single retry is not pre-empted. A failed
    `Run reviewer models` step (the editor never ran) is reported as
    `reviewers_failed` with per-slot / summariser exit codes
    (`reviewers_failure_evidence.txt`, `AUTOFIX_REVIEWERS_FAILED=true`) rather
    than `editor_empty_noop`; the identical-failure cap's report lists the
    failed runs from the head's `review-autofix-failure:v1` markers
    (`AUTOFIX_FAILURE_MARKER_AUTHOR`) so the intake reads their logs; and a
    support script's self-named error line (`untrusted_process_sandbox: …`)
    counts as the crash file when that script exists. When a PR in this repo
    closes, the `heal-pr-reconcile` job (`internal-cancel-on-pr-close.yml`,
    `scripts/workflow_failure_heal_pr_reconcile.sh`,
    `WORKFLOW_HEAL_PR_RECONCILE_ENABLED`) closes the heal PRs stacked on its
    head branch and their heal issues (source unmerged), or merges its final
    head and its base into their heal branches (fast-forward push, no force)
    and re-points them at its base (source merged). Reporters wrap
    the report as `client_payload: {schema_version, report}` (GitHub caps
    `client_payload` at 10 top-level properties); the intake unwraps it and
    accepts the flat shape too, and a rejected dispatch logs `detail=` with
    the first 300 characters of the API error. On by
    default; disable per repo via `WORKFLOW_HEAL_ENABLED=false`; never pushes
    code itself. An autofix report with no failed job reads the review job
    (`codex-agent`, including `codex-agent (claude-branch-review)`), whose run
    concludes success, and the diagnosis prompt
    gets step-sliced logs (`scripts/workflow_failure_heal_evidence.py
    slice-log`: step table, ±80 lines around each `##[error]`, the failing
    step's env, the working-tree / summary groups); the fingerprint still uses
    `filter_log`. Clarify, plan and implement add a **workflow-heal evidence
    folder** for a trusted `ai:workflow-heal` issue (`collect`: sliced job
    logs, fixed diagnostic labels and numeric exit codes from allowlisted artifact
    files (free-form stderr and environment assignments dropped; older cached
    artifact formats are re-collected), provenance, lineage with whether each
    fix reached `main`, runs on the failing head, rate limit / OpenRouter key
    status; 400 KB, fetched separately by each stage without an Actions cache,
    which pull-request runs can read). Cross-repo
    reads require the source repo in the intake's consumer registry; missing
    registry data skips them. Run references also require intake-account-only
    authorship and edit history for the heal issue and occurrence comments;
    every run, same-repo included, requires matching API metadata (repo,
    reported head SHA, workflow name, and PR, named PR or head branch; for a
    source PR, an explicit different linked PR cannot be overridden by a
    matching branch, while source issues can have a separate implementation PR)
    and a failed conclusion (a successful or unfinished run only counts as its
    review job). Unverifiable runs are skipped before log/artifact reads, including
    when previously collected; rejected intake-origin
    references are listed with reasons under `Skipped` in `INDEX.md`. The
    folder is mounted read-only at `/evidence` in the clarify sandbox, and
    the prompt points to its index
    (`=== WORKFLOW HEAL EVIDENCE (UNTRUSTED) ===`). Plan and implement receive
    only the bounded `diagnostics.json` structured prompt section; raw job and
    artifact files are not linked in those prompts, and free-form step names
    and error signatures are represented only by SHA-256 fingerprints. Their
    editor launches scrub GitHub/Telegram credentials and the raw-evidence
    directory and runner environment-file pointers, and temporarily hide checkout
    git auth. Post-editor implementation commits and pushes disable Git hooks.
    Hiding/restoring git auth fails the editor step on error; restoration
    verifies the workflow repository identity, not merely the GitHub host.
    Heal-evidence implement runs pin the issue/plan scope allowlist before the
    editor; preflight and commit ignore scope bypass variables and block empty
    allowlists. This is not a same-uid process isolation boundary.
    Stable log prefixes:
    `WORKFLOW_HEAL_REPORT`, `WORKFLOW_HEAL_AUTOFIX_REPORT`,
    `WORKFLOW_HEAL_PR_RECONCILE`, `WORKFLOW_HEAL`, `WORKFLOW_HEAL_EVIDENCE`.
    A report whose failure reason is `identical_failure_cap`, or a generation
    > 1 of its lineage, is deterministic (`is_deterministic_failure`): the
    intake never files it as `transient` (remaps to `inconclusive`,
    `classification_remapped … reason=deterministic_failure`), and its issue
    body forbids retry / backoff / re-run fixes and requires a regression test
    that reproduces the failure. The review/autofix failure comment names the
    failed step (one jobs-API call matched on `RUNNER_NAME`) and the first
    specific `::error::` line of the captured stage stderr (including the
    resolver's, `resolver_stage_stderr.txt`), redacted
    (`failure-headline`, log prefix `AUTOFIX_FAILURE_HEADLINE`).
15. **Claude issue implementer (retired)** — standalone issues always run the
    Codex pipeline (clarify → plan → implement); `clarify.yml` no longer
    routes to Claude, and `AI_ISSUE_IMPLEMENTER` is no longer read. The
    Claude issue intake / queue / pickup / dispatcher machinery and its
    `ai:claude*` labels were removed (see the `retired_labels` bullet under
    "Implement scope-lock label").

Planner scope note: the Boil the Lake rule is a planner-side instruction for
choosing the right scope mode up front, while CLAUDE.md §5 / the unattended
minimal-change rules still bind implementers and reviewers after that choice is
made. There is no conflict: planners may explicitly choose Expansion or
Selective Expansion when the marginal completeness cost is small, and editors /
reviewers must then stay surgical inside that approved scope rather than
shrinking it ad hoc.

CI guard contract: `.github/workflows/ci.yml` runs the `Shared shell-block
anti-regression checks` step immediately after checkout in the `static-checks`
job, before Python setup, dependency installation, and lint; the test jobs run
in parallel with it (see "CI job layout" below). Rejections emit the secret-safe
`CI_GUARD_FAILURE` diagnostic with `guard`, `check`, `file`, `line`,
`expected`, and `scanned_files` fields, and the guard deliberately uses
ubiquitous `grep` instead of `rg` so runner images without ripgrep still fail
only on real policy drift.

The consumer merged-PR hook (`workflow-templates/.claude/hooks/pr_merge_status_guard.py`)
checks an adjacent unquoted numeric token before `>` as a file descriptor
(`git push origin 2>/dev/null` still checks the current branch); a separated
or quoted number is a push refspec (`git push origin 2 > /dev/null` checks
branch `2`). An unresolved push destination or source prompts for confirmation
without substituting the checked-out branch for the unknown target; independently
resolved refspecs are still checked and blocked when they stack on merged history.
A bare branch after `git push --repo=origin` is checked as a refspec, including
when followed by other bare refspecs. A configured remote supplied positionally
overrides `--repo` and causes the checked-out branch to be checked, even when a
local branch has the same name as that remote.

The `gh api` permission guard in `.claude/hooks/gh_api_write_guard.py` and
its `workflow-templates/` twin exempts an unquoted literal-ID loop counter
only when the entire loop passes the read-only body validator. Unvetted
loops with unquoted `gh api` arguments still prompt; the two hooks must stay
byte-identical (`tests/test_gh_api_write_guard.py`). Shell-rewrite hazards
inside a loop prompt even when the loop counter is not expanded.

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

- New AI-pipeline identifiers must be created through the canonical helpers
  in `scripts/ai_memory_lib.py`; use `make_record_id(prefix)` unless this
  section documents a purpose-built alongside format. Do not hand-roll new
  ID formats in caller modules. The Phase 5 plan's
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
- `make_deterministic_record_id(prefix, *identity_parts)` is the sanctioned
  alongside helper for records that must deduplicate across runs. It emits
  `<sanitized-prefix>-<24hex>` from the SHA-256 digest of the newline-joined
  identity parts; `make_record_id(prefix)` and its format remain unchanged.

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
- The contract's `retired_labels` array lists labels that no longer exist
  upstream (the former `ai:claude*` session labels and `ai:permission-prompt`).
  `scripts/ai_labels.py sync-labels` deletes each one from a repo when present
  (log prefix `LABEL_SYNC_DELETED`; `LABEL_SYNC_RETIRED_ABSENT` when already
  gone; the output gains a `deleted` list). Nothing creates or applies them.


## Implement self-repo staged-support ledger

- `implement.yml` stages the runtime helpers *in-tree* (`install -m … "scripts/${f}"`,
  `prompts/*`, `ai-memory/schemas/*`) from `SCRIPT_REF`, which in this repository is
  `github.sha` (the default branch) while the checkout may be an orchestrator
  integration branch. Those installs overwrite tracked files, and the self-repo
  commit path in `scripts/implement_commit_changes.sh` has no `scripts/` exclusion.
- The staging step inventories every actual worktree install and records each path whose post-stage
  state differs from `HEAD` in
  `STAGED_SUPPORT_LEDGER` (`${RUNTIME_DIR}/staged_support_overwrites.txt`) with the
  installed content under `STAGED_SUPPORT_BASE_DIR`; it also records support
  paths recreated over branch-side deletions. Executable support-ref copies live
  under `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`. All three paths are exported through
  `GITHUB_ENV` and only exist when `github.repository` is this repository.
- `scripts/implement_commit_changes.sh` consumes the ledger before `git add`:
  restore-to-HEAD for untouched copies, 3-way `git merge-file` re-base for
  editor-edited copies, preserve editor-selected modes and branch/editor deletions, remove untouched
  staging recreations, and fail closed on incomplete inputs or merge conflicts.
  The workflow invokes the immutable runtime copy of the commit helper and uses
  runtime copies for all later helper calls, so restoring the worktree cannot
  replace the running script or downgrade post-commit tooling.
  The preflight scope guard resets only ledger paths still equal to their installed
  baseline in its temporary index; editor-modified or deleted paths remain checked.
  Log keys: `IMPLEMENT_STAGED_SUPPORT_LEDGER`, `IMPLEMENT_STAGED_SUPPORT_LEDGER_MISSING`, `IMPLEMENT_STAGED_SUPPORT_RESTORED`,
  `IMPLEMENT_STAGED_SUPPORT_REBASED`, `IMPLEMENT_STAGED_SUPPORT_DELETED_BY_EDITOR`,
  `IMPLEMENT_STAGED_SUPPORT_RECREATED_BY_EDITOR`, `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING`,
  `IMPLEMENT_STAGED_SUPPORT_HEAD_READ_FAILED`, `IMPLEMENT_STAGED_SUPPORT_REBASE_CONFLICT`,
  `IMPLEMENT_STAGED_SUPPORT_REBASE_FAILED`, `IMPLEMENT_STAGED_SUPPORT_RESTORE`.
- Both codex editor launches in `implement.yml` (the "Run Codex implementation"
  attempt loop and the "Attempt post-Codex syntax repair" loop) run
  the immutable `IMPLEMENT_SANDBOX_SUPPORT_DIR` copy of `codex_thread_reuse.sh`
  through
  `env -u STAGED_SUPPORT_LEDGER -u STAGED_SUPPORT_BASE_DIR -u STAGED_SUPPORT_EDITOR_HEAD_LEDGER -u IMPLEMENT_STAGED_SUPPORT_RUN_DIR`.
  The model CLIs run in a credential-free, network-disabled Docker container
  through `implement_untrusted_sandbox.sh`; the host brokers hold credentials
  and only validated edits are transferred back. Both Claude and the Codex
  fallback use the same container. Missing isolation fails closed.
  A preceding env scrub drops GH_TOKEN, GH_PAT, GITHUB_TOKEN, Telegram and
  Actions runtime credentials; `scripts/editor_git_credentials.sh` hides git
  origin/extraheader auth for the editor and restores it after each launch.
  It fails closed (exit 1, log prefix `EDITOR_GIT_CREDENTIALS … reason=…`):
  hide refuses before the editor starts, and restore refuses before any token
  is injected, when a checkout's origin is not its trusted repository or was
  changed, or a pushurl, URL rewrite, proxy, credential helper or include
  directive sits in an editor-writable git config scope.
  The editor never reads those paths; only the restore / reinstall / commit
  steps of the job do. A `pytest` the editor starts to validate its own change
  therefore cannot write fixture paths into the live run's ledgers even when
  the checkout (a review-blocked `ai/reissue-baseline/*` branch, or an
  integration branch that has not synced `main`) carries a `tests/conftest.py`
  older than the session-wide strip. `tests/test_implement_post_codex_recovery.py`
  pins both launch lines and the pass-through of the `CODEX_THREAD_REUSE_*`
  prefix assignments.
- `scripts/implement_staged_support_workspace.sh` (staged into the run dir with the other
  helpers, self-repo only, no-op without a ledger) changes what the *editor* sees:
  `restore` runs right before the Codex implementation loop and before the post-Codex
  syntax-repair loop, puts every ledger path still equal to its installed copy back to
  `HEAD` (removing staging recreations of branch-deleted files) and lists them in
  `STAGED_SUPPORT_EDITOR_HEAD_LEDGER` (`${RUNTIME_DIR}/staged_support_editor_head.txt`);
  `reinstall` runs after each loop and puts the installed copy back for every listed path
  the editor left untouched. The commit helper commits a listed path the editor changed
  as a plain edit (`IMPLEMENT_STAGED_SUPPORT_EDITED_FROM_HEAD`) and never re-bases it.
  Regression: #4113 (run 35072286584) edited `main`'s `scripts/codex_helpers.sh` on
  `orchestrator/project-3965`, the re-base conflicted, and the issue halted in
  `ai:needs-human`. Log keys: `IMPLEMENT_STAGED_SUPPORT_EDITOR_RESTORED`,
  `IMPLEMENT_STAGED_SUPPORT_EDITOR_REINSTALLED`, `IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED`,
  `IMPLEMENT_STAGED_SUPPORT_EDITOR_SKIPPED_PATH`, `IMPLEMENT_STAGED_SUPPORT_EDITOR_RESTORE`,
  `IMPLEMENT_STAGED_SUPPORT_EDITOR_REINSTALL`, `IMPLEMENT_STAGED_SUPPORT_EDITED_FROM_HEAD`.
- Staged-support failures are consumed by the runtime-preserved rejection handler,
  which attempts and verifies the `ai:needs-human` latch, comments with the affected paths and
  latch status, sends the configured CRITICAL alert, and prevents generic diagnosis/re-issue handling.
  Genuine three-way rebase conflicts add
  `<!-- ai:needs-human-latch reason=staged_support_rebase_conflict -->`; missing ledgers,
  baselines, unsafe paths, and merge-tool failures remain human-gated without that marker.
  The source-repository poller's `release_staged_support_needs_human_latches` sweep (gated by
  `STAGED_SUPPORT_LATCH_AUTO_RELEASE_ENABLED`, default `true`) runs in sweep-only mode when no
  tracking project is active and matches that marker or the exact pre-marker #4113 incident
  from run `35072286584`, only on comments from an `OWNER`, `MEMBER`, `COLLABORATOR`, or installed
  `[bot]`, and once the running engine carries
  `scripts/implement_staged_support_workspace.sh` it restores `ai:awaiting-approval` and posts
  `/approved` with a `<!-- ai:needs-human-auto-release reason=staged_support_rebase_conflict
  engine=<sha> -->` marker, at most once per issue per engine commit (log keys
  `STAGED_SUPPORT_LATCH_RELEASED`, `STAGED_SUPPORT_LATCH_SKIP`,
  `STAGED_SUPPORT_LATCH_RELEASE_SKIPPED`). The latest `ai:needs-human` label event must strictly
  precede the staged-support comment and have the same actor; same-second timestamps fail closed,
  so clearing that latch and later setting another human gate cannot reuse the stale marker.
  Immediately before changing labels, the sweep re-reads the paginated live labels and latest
  latch event; a changed or unreadable latch, or a residual `ai:implementing` label, skips release
  for that tick. A failed `/approved` response triggers a paginated history check; a trusted
  release marker confirms an accepted write, while a confirmed failure restores
  `ai:needs-human`. If that compensation also fails, the unresolved latch marker blocks
  managed and standalone auto-approval and raises a CRITICAL alert. Those recovery guards use
  the batched comment cache only when it explicitly reports an available array with fewer than
  100 entries; a missing/partial field or a full window triggers a paginated history read, and
  unavailable or malformed history fails closed for that tick.
  Consumer repositories and other latch reasons stay human-cleared.
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

## Review self-repo support staging runs under main's workflow YAML

- `internal-review.yml` calls the reusable
  `shubhodeep1/coding-workflows/.github/workflows/review_autofix.yml@main`
  (a `uses:` ref cannot vary per PR), while `review_autofix.yml`'s "Resolve
  workflow support ref" step consumes the SHA selected by the gate from
  `toJSON(job)` identity (direct `job.workflow_*` expressions are not supported
  by the pinned actionlint version). A same-repo PR reports its branch and PR
  head in that identity; the gate therefore reads the protected `main` branch's
  SHA independently via the GitHub API and fails closed if protection or SHA
  cannot be confirmed. Consumer `@stable` calls resolve the release tag, and
  consumer SHA pins use their literal pinned value. Every job checks out the
  selected immutable SHA and compares HEAD before running support. Self-repo
  PR-head `scripts/*`, `prompts/*`, and `ai-memory/schemas/*` are reviewed as
  data; the executable runtime bundle comes only from the verified workflow
  commit. Consumer release pins use the same SHA-bound checkout. The
  failure-path reporter skips when support staging did not complete or its
  optional Python helper is absent; neither case executes `scripts/` from
  the PR worktree.
- `internal-review.yml` itself must not forward a `with:` input that
  `review_autofix.yml` on `main` does not define yet: GitHub validates the
  call against `main`'s file, so every review run on the PR adding the input
  ends as a zero-job `startup_failure` (runs 36088124636 through 36095647423
  on PR #4438, `input "claude_fixer_converged_head" is not defined`). Add the
  input to `review_autofix.yml` (and the `@stable`-pinned consumer
  `ai-review.yml`, which moves in lockstep with the release), and in this
  repo dispatch `review_autofix.yml` directly for it.
- Consequence for contributors and the unattended editor: a helper on the PR
  branch may not depend on a new workflow export until that export is on
  `main`. New variables a staged helper reads must default inside the helper
  (`unattended_system_instructions.md` §8), typically to a
  `${RUNTIME_DIR}/<artifact>` path, and the helper should publish the resolved
  value to `GITHUB_ENV` when later steps consume it.
- Incident: PR #4174 (`ai/issue-4173`, head `662aacb`) added
  `LINKED_ISSUE_METADATA_FILE` as a required env of
  `scripts/review_collect_pr_metadata.sh` together with the matching
  `review_autofix.yml` export. The export never ran, and review runs
  35546298657, 35549937758, 35551938072, and 35552937934 all failed in
  "Collect PR metadata" with `required env LINKED_ISSUE_METADATA_FILE is
  unset` while the stall poller kept re-dispatching. `main` now exports the
  variable as well, so the same path is defined on both sides.
- `MAIN_PRIMARY_BOOTSTRAP_SCRIPTS` remains a registry for compatibility;
  it is staged from the verified workflow SHA, not a moving `main` checkout.
  Changes to a PR's support scripts take effect in reviews only after a
  trusted workflow update. Missing required runtime scripts fail closed.
  The existing `STAGE_MAIN_PINNED_DIVERGENCE` notice still compares PR data
  against trusted files but never selects the PR copy for execution.
- Editor preconditions are checked before the reviewers: the preflight step
  runs `scripts/review_apply_fixes.sh --preflight` (`REVIEW_EDITOR_PREFLIGHT`,
  kill switch `REVIEW_EDITOR_PREFLIGHT_ENABLED`). A new `: "${VAR:?…}"` guard
  in that script must also be listed in `review_apply_fixes_preflight()`
  (contract-tested), and the variable must already be set when the preflight
  step runs.

## Workflow file size limit

- GitHub does not start runs for a workflow file over **512,000 bytes**
  (500 KiB). Measured on 2026-09-24 with padded probe workflows: 512,000
  bytes ran, 512,001 did not. Nothing reports an error. Every push instead
  creates a zero-job run named after the file path (for example
  `.github/workflows/review_autofix.yml`) that concludes `failure` with
  "This run likely failed because of a workflow file issue", and a reusable
  workflow over the limit cannot be called.
- Incident: #4327 grew `review_autofix.yml` from 505,283 to 540,537 bytes.
  The phantom runs matched the `test-and-mark-stable.yml` Phase 4 review-run
  regex on the pinned head SHA, Phase 4 accepted one as "completed with
  failure", and Phase 4b failed with `retry_timeout` (stable gate run
  35903885958). Phase 4 now drops runs whose `name` equals their `path`,
  which only happens when GitHub cannot load the workflow, since every
  workflow here and in `workflow-templates/` sets `name:`.
- Guard: `tests/test_workflow_file_size_limit.py` (CI step "Review autofix
  step-script contract and workflow file size guard tests") fails when any
  `.github/workflows/*.yml` reaches **480,000 bytes**, 32,000 bytes before
  the hard limit.
- **Split rule, for interactive sessions and the unattended pipelines alike**
  (also `CLAUDE.md` §27 and `unattended_system_instructions.md` §24):
  when a change leaves a workflow file at or above 480,000 bytes, move the
  largest inline `run:` bodies into `scripts/` **in the same PR** until the
  file is well under the guard (aim for 50,000+ bytes of headroom). Never
  raise the guard threshold and never split a workflow into a second
  workflow file to get under it. For `review_autofix.yml`, follow the
  existing pattern:
  - Move the body verbatim to a new `review_autofix_step_<slug>.sh` under `scripts/` with
    the standard comment header (shebang plus a comment naming the step),
    mode `0755`. The body must contain no `${{ }}` expression: GitHub only
    substitutes those inside the workflow file, so pass the values through
    the step's `env:` first.
  - Keep the step's `name:`, `id:`, `if:`, `env:` and `continue-on-error:`
    in the workflow (§6). Replace `run:` with the resolving wrapper, which
    tries `${SUPPORT_SCRIPTS_DIR}`, then
    `${GITHUB_WORKSPACE}/.codex-workflow-src/scripts`, then
    `${GITHUB_WORKSPACE}/.codex-workflow-src-main/scripts`, and `source`s
    the script in the step shell. A missing script fails the step with
    `::error::`; only `always()` steps, which also run after support staging
    failed, skip with `::warning::` instead.
  - Add the script to `REQUIRED_BOOTSTRAP_SCRIPTS` in
    `scripts/stage_workflow_support.sh`, to the `REVIEW_AUTOFIX_STEP_SCRIPTS`
    registry in `tests/review_autofix_step_scripts.py`, and to
    `docs/INVENTORY.md`.
  - Contract tests keep reading the step bodies through
    `expanded_review_autofix_text()`, which inlines each script again, so
    their assertions do not change.
  - For other workflows, move the body to a `scripts/` file that the job
    already stages or checks out, and invoke it the same way.
- The `.codex-workflow-src` fallback is the verified workflow-commit checkout;
  a missing required script never falls back to a PR-head or moving-ref copy.

## Test-suite git environment isolation

- `tests/conftest.py` strips the repo-pinning git variables (`GIT_DIR`,
  `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`,
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`) from `os.environ` for the whole pytest
  session and restores them afterwards. Every git subprocess a test spawns
  therefore resolves its repository from `cwd`, or from variables the test
  sets itself.
- Why: implement.yml, review_autofix.yml, and validate.yml export `GIT_DIR` /
  `GIT_WORK_TREE` into `$GITHUB_ENV` ("Activate workspace shell context"), and
  the codex editor inherits them when it runs `pytest`. A scratch-repo test
  that only set `cwd` was rebound to the live checkout: during the implement
  run for issue #4092, `tests/test_assemble_changelog.py` produced commit
  `37c72a5` ("base", author `test <test@example.invalid>`) on `ai/issue-4092`,
  reverting 1,654 lines of runtime helpers, and PR #4093's review editor then
  failed with `model_provider_broker_start: command not found` (run
  34982425230).
- Per-test scrubs that also drop `BASH_ENV`, `ENV`, or `WORKSPACE_PATH` stay
  as they are; the conftest fixture is the floor, not a replacement.
  `tests/test_pytest_git_env_isolation.py` pins the contract with a nested
  pytest run against a sentinel repository.
- The same fixture strips the self-repo staged-support ledger paths that
  implement.yml's "Stage workflow support files" step exports into
  `$GITHUB_ENV` (`STAGED_SUPPORT_LEDGER`, `STAGED_SUPPORT_BASE_DIR`,
  `STAGED_SUPPORT_EDITOR_HEAD_LEDGER`, `IMPLEMENT_STAGED_SUPPORT_RUN_DIR`;
  `STAGED_SUPPORT_RUNTIME_ENV_VARS` in `tests/conftest.py`).
  `scripts/implement_staged_support_workspace.sh` and
  `scripts/implement_commit_changes.sh` read `STAGED_SUPPORT_EDITOR_HEAD_LEDGER`
  from the environment before their ledger-relative default, so a test that
  copies `os.environ` and only overrides `STAGED_SUPPORT_LEDGER` /
  `STAGED_SUPPORT_BASE_DIR` writes its fixture paths into the live run's
  editor-head ledger. Incident: implement runs 35614385686, 35628923735,
  35642366131 and 35656715219 (issues #4227 / #4242, project #4139) each
  finished the editor with a complete change set, then the editor's own
  pytest run of `tests/test_implement_post_codex_recovery.py` appended a
  fixture-only `scripts/` + `helper.sh` path to
  `/tmp/codex-implement-<run>/staged_support_editor_head.txt`, and the
  post-editor `reinstall` failed closed with
  `IMPLEMENT_STAGED_SUPPORT_BASE_MISSING` for that path. The stall
  poller retried twice, the stall judge re-issued #4227 as #4242, and the
  replacement failed the same way. The second nested run in
  `tests/test_pytest_git_env_isolation.py` pins this contract against a
  sentinel ledger. The conftest strip only protects checkouts that carry it: #4242's
  runs 35656715219 / 35668070395 checked out `orchestrator/project-4139`
  and the preserved `ai/reissue-baseline/pr-4174-*` head predates it, so
  `implement.yml` also drops the four variables from the editor's
  environment at both launch sites (see "Implement self-repo staged-support
  ledger").

## Models in use (defaults; overridable via repo-vars)

| Phase | Default model | Default reasoning | Verbosity | Engine · Claude role |
|---|---|---|---|---|
| clarify, clarify-respond | `openai/gpt-6-sol` | `high` (smoke: `low` — `clarify.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` | Claude (Opus 5.5; codex fallback) · `CLARIFY`, `CLARIFY_RESPOND` |
| plan | `openai/gpt-6-sol` | `high` (smoke: `low` — `plan.yml`'s "Detect smoke test" step sets `MODEL_REASONING_EFFORT=low`) | `low` | Claude (Opus 5.5; codex fallback) · `PLAN` |
| orchestrate (decompose), judge | `openai/gpt-6-sol` | `high` | `low` | codex · `ORCHESTRATE`, `WAVE_JUDGE`, `STALL_JUDGE`, `INTEGRATION_JUDGE`, `SECURITY_JUDGE` |
| implement (main editor) | `openai/gpt-6-sol` | `high` (smoke: no override — see `.github/workflows/implement.yml:597-606`) | `low` | Claude (Opus 5.5; codex fallback) · `IMPLEMENT` |
| implement-repair, implement-repair-syntax | `openai/gpt-6-sol` | `high` | `low` | Claude (Opus 5.5; codex fallback) · `IMPLEMENT_REPAIR` |
| implement-diagnose | `openai/gpt-6-sol` | `high` | `low` | Claude (Opus 5.5; codex fallback) · `IMPLEMENT_DIAGNOSE` |
| review autofix editor | `openai/gpt-6-sol` | `high` (smoke: `medium`) | `low` | OpenCode · `REVIEW_EDITOR` |
| review autofix reviewers (pass 1) | `REVIEWER_MODELS` (default roster: `minimax/minimax-m3`, `z-ai/glm-5.2`, `deepseek/deepseek-v4-pro`, `google/gemini-3.1-flash-lite`, `qwen/qwen3.7-plus`, `openai/gpt-6-luna`) | `xhigh` per reviewer call (hardcoded at the `run_reviewer_pass ... "xhigh"` callsite in `scripts/review_run_reviewers.sh:4733`; not affected by the smoke `REVIEWER_REASONING_EFFORT=low` override in two-pass mode) | `low` | OpenCode only (no engine switch) |
| review autofix reviewers (pass 2) | `REVIEWER_MODELS` (same roster, after pass-2 scope / tier filtering) | `high` on diffs below `REVIEWER_PASS2_DIFF_LARGE_LOC=200`, `xhigh` at or above that threshold; smoke: `low`; operator override wins | `low` | OpenCode only (no engine switch) |
| review consolidator | `openai/gpt-6-sol` | `high` | `low` | OpenCode · `REVIEW_CONSOLIDATOR` |
| conflict resolver | `openai/gpt-6-sol` | `high` (decoupled from smoke; `scripts/review_conflict_resolve.sh` validates `xhigh`, `high`, `medium`, `none` only — `low` is rejected; default lowered from `xhigh` after runs `25627236793` / `25627316961` hit `timeout`-killed retries on degenerate orchestrator-stack integrations; override per-repo via `vars.THINKING_LEVEL_CONFLICT_RESOLVER`) | `low` | OpenCode · `CONFLICT_RESOLVER` |
| validate generate, diagnose | `openai/gpt-6-sol` | `high` | `low` | codex · `VALIDATE` |
| validate discover | `openai/gpt-6-sol` | `high` (per-phase override via `MODEL_REASONING_EFFORT_DISCOVER`) | `low` | codex · `VALIDATE` |
| validate fix-harness, self-heal | `openai/gpt-6-sol` | `high` | `low` | codex · `VALIDATE_SELF_HEAL` |
| workflow log analyze | `openai/gpt-6-sol` | `xhigh` | `low` | codex · `LOG_ANALYSIS` |
| workflow audit | `openai/gpt-6-sol` | `xhigh` (hardcoded in `.github/workflows/workflow-log-analysis.yml:716-717`) | `low` | codex · `LOG_AUDIT` |
| workflow api-redundancy | `openai/gpt-6-sol` | `high` (default of `THINKING_LEVEL_ANALYSIS`) | `low` | codex · `LOG_ANALYSIS` |
| workflow log summary | `openai/gpt-6-luna` | default | `low` | OpenCode · `LOG_SUMMARY` |
| reviewer consensus summariser | `openai/gpt-6-luna` | `medium` (`XPOLL_SUMMARISER_REASONING`) | `low` | OpenCode · `SUMMARISER` |

The **Engine · Claude role** column names today's engine and the role name
`scripts/ai_engine.sh` resolves for that row (README "Claude engine").
Every role's default in `.github/ai/claude_engine.json` is `codex`
until its cutover (Phase 5a moved `CLARIFY`, `CLARIFY_RESPOND` and `PLAN`
to `claude`, Phase 5b `IMPLEMENT`, `IMPLEMENT_REPAIR` and `IMPLEMENT_DIAGNOSE`; a missing config file still means codex for every role);
`AI_ENGINE_<ROLE>`, `AI_ENGINE` or the `ai:engine-claude`
/ `ai:codex` labels select it per run. On Claude a role uses its existing
model variable only when that value starts with `claude-`, else Opus 5.5
(`claude-opus-5-5`), or Sonnet 5.5 (`claude-sonnet-5-5`) for `LOG_SUMMARY`,
`RETRO`, `MATERIALITY`, `SUMMARISER` and `BEHAVIOURAL_SMOKE`; the reasoning
column is the effort (`none` / `minimal` → `low`). The reviewer rows have no
engine switch. When Claude is unavailable (`claude_run` exit 75,
`AI_ENGINE_FALLBACK`), the run uses the codex/OpenCode path unchanged. The
pinned CLI is `@anthropic-ai/claude-code` `cli_version` from the same file,
installed by `.github/actions/install-claude`.

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

All gpt-6-sol phases now resolve to `low` verbosity at every layer: the per-phase
`MODEL_VERBOSITY` env-var default in `.github/workflows/*.yml` (`VERBOSITY_*`
repo-vars), the `-c model_verbosity=low` CLI flag on every `codex exec`
callsite (≈20 sites across `scripts/*.sh` and `.github/workflows/*.yml`),
the `model_verbosity = "low"` line that `scripts/write_codex_config.sh:242`
writes into `config.toml`, and the `"default_verbosity": "low"` for
`openai/gpt-6-sol` in `scripts/codex_model_catalog.json`. Third-party
reviewer models (`minimax/minimax-m3`, `z-ai/glm-5.2`,
`deepseek/deepseek-v4-pro`, `google/gemini-3.1-flash-lite`,
`qwen/qwen3.7-plus`) carry `support_verbosity = false` in the catalog
(the `openai/gpt-6-luna` reviewer slot supports it, with catalog default
`low`) — codex CLI logs
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

Every editor / consolidator / resolver phase now defaults to `openai/gpt-6-sol`.
Reviewer fan-out remains driven by the `REVIEWER_MODELS` roster in
`.github/workflows/review_autofix.yml` (currently the models listed in the
table above). The previous legacy editor split (patch-heavy
phases on a separate older slug) was retired after the announce-without-emit
regression (openai/codex#11151) drove repeat no-edit failures. The
2026-05-07 ablation suite then identified the underlying root cause as
`apply_patch_tool_type: "freeform"` on the OpenRouter Responses path (see
the `openai/gpt-5.4` catalog entry — `apply_patch_tool_type` is now
`function`).

The reviewer-only multi-model run (claude-branch-review) uses the same
reviewer models (`minimax/minimax-m3`, `z-ai/glm-5.2`,
`deepseek/deepseek-v4-pro`, `google/gemini-3.1-flash-lite`,
`qwen/qwen3.7-plus`, `openai/gpt-6-luna`) plus
`unattended_system_instructions.md` as system context.

---

## Interactive slash-command model selection

**Interactive Claude Code sessions only.** This section is unrelated to
`## Models in use` above: that table covers the unattended codex/OpenCode
pipeline models, which read `unattended_system_instructions.md` and are
driven by repo-vars.

The 13 commands in `.claude/commands/*.md` deliberately carry **no**
`model:` frontmatter, and no `context:` / `background:` keys either. Every
`/command` runs on the model the operator picked for the session (via
`/model` or the session's configured model), for every turn of the command.
Do not re-add per-command `model:` pins: they were tried (PRs #3967 and
#3971) and removed because the operator's session choice should decide the
model, not the command file. A file that starts with `---` would be parsed
as frontmatter, so the command body must remain the first line.

`/implement-plan-claude` and `/implement-issue-claude` are thin hand-offs
that run in the operator's session on the model the operator picked, like
every other command: `/implement-plan-claude` dispatches the AI orchestrator
(the `/implement-plan-ai` procedure) and marks the project `ai:engine-claude`,
and `/implement-issue-claude` labels one standalone issue `ai:engine-claude`
and posts `/reclarify`. Neither implements anything in the session, opens a
PR, or starts other sessions. The session-driven chain they used to run
(stage sessions, the Sonnet checker, hand-back Routines, the Claude issue
pickup and its session sweep, auto-decisions, the `docs/implement-plan/`
logs) was retired on 2026-10-03 together with CLAUDE.md §26 / §28; see
`docs/plans/replace-claude-sessions-with-cli-engine-plan.md`.

No field here changes what any consumer repo receives on the `@stable`
sync: `.claude/commands/` is not part of the synced surface, and the
template copies under `workflow-templates/.claude/commands/` have never
carried frontmatter.

### Live `.claude/` copies and their templates

This repo runs its own `.claude/` copy of the files it ships to consumers
under `workflow-templates/.claude/`. The pipeline's editors cannot edit
`.claude/**`, so an AI fix that changes only the template leaves the live copy
behind; #6133 (the merged-PR guard hook) and #6176 (four command files) broke
`main` that way. Three pieces keep the pairs in step:

- `tests/test_claude_template_live_parity.py` (own `ci.yml` step) fails when a
  template and its live copy differ in content or executable bits, unless the
  file is listed in `.github/ai/claude_template_divergence.json` with a reason.
  Six command files are listed today; both copies of those are edited by hand.
- `scripts/sync_claude_live_copies.py` runs from
  `.github/workflows/sync-claude-live-copies.yml` on every push to `main` that
  touches `workflow-templates/.claude/**`. When the push changed a template
  but not its live copy, it copies the template over, pushes
  `ai/sync-claude-live-copies` and opens a PR (or refreshes the open one). It
  also carries forward still-drifted live copies from the existing sync branch
  only when that branch's copy matches the current template; a live copy
  changed on `main` since the earlier sync is left for the parity test.
  The auto-merge-eligible branch accepts a path only when every template
  commit since the last live-copy edit is associated with a merged PR from a
  non-`ai/*`, non-`orchestrator/*`, non-`auto/*` branch targeting the sync base
  from a same-repository head, authored by an OWNER/MEMBER/COLLABORATOR
  non-bot account, and neither its subject nor any PR commit subject carries
  a pipeline marker or a squash `(#N)` suffix. A trusted collaborator's
  account or session can still submit AI-written content.
  Unverifiable paths instead go to `ai/sync-claude-live-copies-held` as a draft
  PR for human review, with a Telegram WARNING listing their source commits.
  Malformed merge timestamps also fail authorization and hold the path.
  A ready held PR is converted back to draft before a refresh pushes content.
  Logs add `CLAUDE_LIVE_SYNC authorized`, `held`, and `converted_to_draft`;
  the history cap defaults to 30 distinct commits per run
  (`CLAUDE_LIVE_SYNC_MAX_PROVENANCE_COMMITS`).
  Branch replacement is lease-checked; push or PR API failures fail the job
  with a structured `CLAUDE_LIVE_SYNC error` line, leaving the branch for a
  later sync attempt.
  Its existing-PR lookup accepts only an open PR from this repository's sync
  branch into the configured base branch, even when other PRs are returned.
  It fails open on an unusable `before` commit: no sync is attempted, and
  the parity test still reports the drift. Provenance and open-PR reads use
  GraphQL: one aliased association lookup covers up to 30 distinct template
  commits, followed by 1-3 pages per distinct merged PR and one sync-PR lookup
  per nonempty group.
  Only PR creation uses REST, at most once for each of the authorized and held
  groups (two REST calls total). `GH_PAT` remains broad to trigger CI/review;
  branch names and commit subjects are not proof of human authorship, and
  marking a held PR ready between conversion and push is a residual race. Log
  prefix `CLAUDE_LIVE_SYNC`.
- A push that changed only the live copy is left to the parity test.

For PRs targeting `main`, the `tests-hooks-and-orchestrator` CI job runs
`sync_claude_live_copies.py sync --dry-run` in its disposable checkout,
using the PR base SHA and full git history. This prepares only eligible
template-only command changes for the tests without committing or pushing live
files. Security hooks under `.claude/hooks/**` and `.claude/settings.json` are
never prepared: their committed live copies must match the templates for CI
to pass, even when the editor cannot write the live file.
Changes to both halves that still differ remain test failures. Push CI and
PRs targeting `stable` check the committed tree without preparation, so drift
on `main` is still reported while the post-merge sync PR is pending.
Both release gates' `validate-scripts` jobs also run
`tests/test_claude_template_live_parity.py` on the committed tree: drift,
including a pending or held sync PR, blocks the `stable` release until both
copies match or the file is allowlisted.

New templates without a live copy are also treated as drift and copied into
`.claude/` (including new subdirectories) with their executable permissions.
Template symlinks (including directory links) and symlinks anywhere in a live
destination path fail the sync/parity check rather than being followed; the
workflow must not copy checkout-local credential files into a PR.
A later template push recovers an earlier failed or superseded sync only if
the template's most recent change is newer than the live file's; an equal or
newer live edit is left untouched.
An unusable `before` commit still skips the sync, so CI reports any drift.

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
PROFILE.name=core manifest=workflow-templates/profiles/core.txt wrappers=ai-clarify.yml,ai-plan.yml,ai-implement.yml,ai-review.yml,ai-issue-pr-status.yml,ai-cancel-on-pr-close.yml,ai-orchestrate-clarify-respond.yml
PROFILE.name=standard manifest=workflow-templates/profiles/standard.txt wrappers=ai-clarify.yml,ai-plan.yml,ai-implement.yml,ai-review.yml,ai-issue-pr-status.yml,ai-cancel-on-pr-close.yml,ai-orchestrate.yml,ai-orchestrate-poll.yml,ai-orchestrate-clarify-respond.yml,ai-validate.yml,ai-sync-labels.yml,review_rb_judge_dispatch.yml
PROFILE.name=full manifest=workflow-templates/profiles/full.txt wrappers=ai-cancel-on-pr-close.yml,ai-check-failure-triage.yml,ai-clarify.yml,ai-implement.yml,ai-issue-pr-status.yml,ai-memory-maintenance.yml,ai-orchestrate-clarify-respond.yml,ai-orchestrate-poll.yml,ai-orchestrate.yml,ai-plan.yml,ai-review.yml,ai-security-audit.yml,ai-sync-labels.yml,ai-update-workflows.yml,ai-validate.yml,ai-workflow-failure-heal.yml,review_rb_judge_dispatch.yml

## Immutable consumer wrapper pins

- Canonical `workflow-templates/*.yml` files retain `@stable` as a delivery-time
  render token. Installed consumer wrappers must use
  `@<40-character-release-sha> # stable` instead.
- `scripts/workflow_wrapper_refs.py` is the single renderer used by the updater,
  drift audit, and `/seed-repo`; do not duplicate its substitution rules.
- `.github/workflows/update_workflows.yml` renders every top-level wrapper before
  any consumer mutation, updates an existing `ai-update-workflows.yml` regardless
  of profile, and never creates that self-updater when absent.
- Its `Remove retired upstream files` step reads
  `workflow-templates/retired_files.txt` (`.claude/` files upstream no longer
  ships) and deletes a consumer copy only when it is byte-identical to a
  released version (log prefix `RETIRED_FILE_REMOVED`); a locally modified copy
  is kept (`RETIRED_FILE_KEPT_MODIFIED`).
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
  Validate's four hooks run from trusted support in a tokenless, network-disabled
  container against a bounded, screened workspace copy, never the host checkout
  or its `.git`. Only non-executable, simple-name `.txt` data under
  `validation/hook-output/<hook>/` may be replayed to the host; it must not be
  sourced or executed. Any other changed or deleted path rejects the entire
  replay as an isolation/transfer failure, stopping validation even for
  nonfatal hooks. An explicit `validate.yml` `target_ref` (a `claude/implement-plan-*` branch)
  requires exactly one open trusted-author same-repo project PR targeting the
  default branch; any other base is refused. The stacked project-branch and
  `stable` targets of the retired `/implement-plan-claude` chain (#4734,
  #4791) were removed. Checkout
  pins and verifies that PR's SHA without persisting checkout credentials.
  Empty `target_ref` retains integration/default selection.

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
waived_findings=<count> fix_cycle_diff_entries=<count> fix_cycle_diff_files=<count>`.
Incident: tele-funtoken-msg-scoring#3928, fun-token-multi-chain#471 and
binance-blessings#249 each merged every fix issue, never repeated a finding ID
between cycles, and still exhausted the budget on fresh full-range samples.
A delta re-audit also sees the code the previous fix cycle wrote as newly
introduced attack surface. `run_security_pass_inline` computes the current
fix-cycle diff entry `{cycle, since_sha, head_sha, files}` (range files changed
between `security_pass_last_audited_sha` and the head, added/modified only,
capped at 200 files) from the local checkout, carries the previous cycle's
entry over exactly once from `security_pass_fix_touched_files` (entries with
`cycle >= security_pass_cycle - 1`; older and malformed rows are dropped by
`ensure_security_pass_state_fields` and at read time), and hands the list to
`scripts/security_audit.sh` as `SECURITY_AUDIT_FIX_CYCLE_DIFFS`. The engine
keeps those files in scope and appends their unified diff hunks to the prompt
between `=== BEGIN/END UNTRUSTED FIX-CYCLE CODE ===` fences with rules to audit
them as fresh attack surface for new defect classes (readiness and
state-transition predicates, money-state transitions, idempotency fences), in
addition to the unchanged prior-findings rules. Hunks are capped by
`SECURITY_AUDIT_FIX_DIFF_MAX_LINES` (`1200`) and
`SECURITY_AUDIT_FIX_DIFF_MAX_BYTES` (`96000`); past the cap files are listed by
name only. The current entry is written by the same `jq` that records
`security_pass_head_sha` (a blocked result keeps at most the last 3 entries, a
clean result empties the list), and `/re-security-pass`, `/security-pass-waive`
in the failed state, the exhaustion judge's accept-all path, and the
`ENABLE_SECURITY_PASS=false` release clear it with the reported findings.
Every step fails open with a `::warning::` and today's behaviour; no GitHub
API call is involved. Incident: tele-funtoken-msg-scoring#4281 exhausted 3/3
cycles with every fix merged and no repeated finding because the last finding
sat in `_season_pool_settlement_readiness`, a predicate cycle 1's fix created
and cycles 2 and 3 were never told to audit as new code.
Persistent findings after `MAX_SECURITY_PASS_CYCLES` (default `5`)
terminalize as `ai:security-pass-failed`; `/re-security-pass` resets the
bounded loop and the next audit covers the full range again.
The terminal state is engine-aware: every terminal path
(`security_pass_terminal_failure`, `security_pass_closed_fix_failure`,
`security_pass_fix_reissue_exhausted`) records the engine commit that ran the
tick in `security_pass_failed_engine_sha` (`ORCHESTRATOR_ENGINE_SHA`, resolved
once at startup by `resolve_orchestrator_engine_sha` from `ORCHESTRATE_ENGINE_SHA`
or the `.codex-workflow-src` HEAD; empty when unresolvable). With
`SECURITY_PASS_AUTO_RESET_ON_ENGINE_CHANGE` (default `true`), a tick whose
engine differs from that record, with no `/re-security-pass` comment claiming
the tick, performs the same reset once per engine commit
(`security_pass_auto_reset_engine_shas`, last 20 kept, both fields normalized by
`ensure_security_pass_state_fields`), logs `SECURITY_PASS_AUTO_RESET` or
`SECURITY_PASS_AUTO_RESET_SKIPPED ... reason=engine_unresolved|same_engine|already_reset_on_engine`,
and posts a `<!-- security-pass-auto-reset:<sha> -->` tracking comment. A
legacy state without the record counts as a different engine, so projects
parked by older engines (binance-blessings#249) re-run on their first tick
after a sync; the same engine failing a project again never re-fires.
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
another project or another cycle is never adopted. The review-blocked judge's
`close_and_reissue` replacement carries those markers as well:
`scripts/review_rb_judge.sh` copies the parent's `**Orchestrator metadata**`
lines (tracking issue, integration branch, local ID, priority, managed-by) only
when its tracking and branch lines agree with the GitHub-reported
`orchestrator/project-<n>` PR base. Missing, malformed, or inconsistent metadata
falls back to base-derived tracking and branch lines without an unverified local
ID. The validated block is placed ahead of the review-blocked footer
(`REISSUE_ORCHESTRATOR_METADATA_CARRIED` / `_ABSENT`), and its spot-fix
`files_touched` allowlist unions the judge's cited files with the closed PR's
changed files that still exist at its head (`REISSUE_FILES_TOUCHED_UNION`,
fail-open on a failed `pulls/<n>/files` listing), then with the new files the
judge declares in `new_output_paths` (`REISSUE_FILES_TOUCHED_NEW_OUTPUTS`).
A declared path is kept only when it passes the path validator, is
printable ASCII with no leading or trailing space (the scope guard trims
entries and splits lines on Unicode separators), carries no
glob character or trailing `/`, has no `.git` segment (any depth or
letter case), does not exist at the
closed head (a failed lookup there skips it too), and ends in a segment with
a file extension (`no_extension` otherwise: the scope guard lets a bare entry
cover everything beneath it, so a new directory-shaped path would exempt a
new subtree); at most 10 are read,
and a rejected one is skipped, never a fallback to `redo`. Incident: #4664's
reissue needed a new changelog fragment and four new fixtures that neither
source could list (heal #4665).
Before that block is appended, canonical tracking-issue, integration-branch,
and local-ID lines are removed from the judge-generated issue prose, so only
the PR-base-validated block can supply successor-adoption lineage. Incident:
binance-blessings#249 / #294, 2026-09-19. An inconclusive lookup
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
decision table. With `SECURITY_PASS_ADVISORY_DEFER_UNTIL_MERGED` (default `true`) the
accepted finding's advisory follow-up is not filed at judge time: the waiver
row keeps `followup_pending: true`, `audited_head_sha`, and the `finding`
payload (`SECURITY_PASS_ADVISORY_FOLLOWUP_DEFERRED`), and
`security_pass_file_deferred_advisory_followups`, called from every site that
records `final_merge_status = "merged"`, files it once the integration branch
is on the default branch (`SECURITY_PASS_ADVISORY_FOLLOWUPS_FILED`, body line
naming the merging PR, `🔐 Security-pass advisory follow-ups filed` comment).
Judge-time filing planned #4090 / #4091 against a `main` that did not yet
contain the broker module the findings cite, so the planner emitted
`BLOCKED: PR #3968 is still open` and both sat in `ai:blocked`. The
`/security-pass-waive` path defers the same way. A create that fails keeps the
row pending for the next merged-state tick; `create_security_pass_advisory_followup`
clears `followup_pending` and drops the payload when it records the issue.
Right before the deferred filer, the same merged-state sites call
`security_pass_unblock_filed_advisory_followups`: each
`security_pass_followup_issues` row whose issue is not in
`security_pass_followups_merge_checked` costs one issue GET; an open follow-up
labelled `ai:blocked` costs one paginated comments GET and gets one
`/answer [auto-answered-by-poller]` comment unless a trusted
OWNER/MEMBER/COLLABORATOR User comment carries both that prefix and its durable
unblock marker (plan.yml moves ai:blocked to ai:planning on `/answer`). Every read
issue is appended to `security_pass_followups_merge_checked` (deduped,
last 100; `security_pass_mark_followup_merge_checked`) so it is never re-read
after a successful state write; failed state writes may repeat the reads, but
the durable comment marker prevents a second `/answer` POST
(`SECURITY_PASS_ADVISORY_FOLLOWUP_UNBLOCKED ... outcome=answered|not_blocked|closed`,
`🔓 Security-pass advisory follow-ups re-planned` comment when any were
answered). The filer records the issues it creates after the merge as checked
at creation, and the completed-project finalizer re-enters both steps while
pending waiver rows or unchecked follow-ups remain. The row shape of
`security_pass_followup_issues` is unchanged. This is what un-parks advisories
filed before the merge (#4090 / #4091) without a human.
`MAX_SECURITY_PASS_KEEP_FIXING_ROUNDS` (default `2`, `0` = unbounded) bounds
how many judge rounds may end in `keep_fixing`: `judge_round` beyond the cap
puts `keep_fixing_available: false` and `max_keep_fixing_rounds` in the
diagnostics, and after verdict normalization the poller rewrites every
`keep_fixing` decision to `accept_with_followup` with the justification
prefixed `[keep_fixing capped after <c> judge round(s); converted to advisory
follow-up]` (`SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED tracking_issue=<N>
round=<r> cap=<c> converted=<n>`), so the accept-all path runs and the project
completes with deferred advisories. `fail` verdicts are untouched; unlike
`MAX_SECURITY_PASS_JUDGE_ROUNDS` this cap never terminalizes. Project #3965
ran fix cycles 6 and 7 on a 5-cycle budget because rounds 1 and 2 each chose
`keep_fixing` and nothing bounded the sequence.
Waivers travel to the engine as `SECURITY_AUDIT_WAIVED_FINDINGS`
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
- `LABEL_SYNC_DELETED`
- `LABEL_SYNC_RETIRED_ABSENT`
- `RETIRED_FILE_REMOVED`
- `RETIRED_FILE_KEPT_MODIFIED`
- `AUTOFIX_PEER_CHECK`
- `AUTOFIX_DISPATCH_SKIPPED`
- `AUTOFIX_DISPATCH_ISSUED`
- `AUTOFIX_GATE_SKIP`
- `AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD`
- `AUTOFIX_GATE_TERMINAL_SAME_HEAD_UNCHECKED`
- `AUTOFIX_GATE_TERMINAL_SAME_HEAD_OVERRIDE`
- `AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED`
- `AI_PHASE_FAILURE_V1`
- `AI_PHASE_GATE_V1`
- `WORKFLOW_SCENARIO_TRACE_WRITTEN`
- `WORKFLOW_SCENARIO_TRACE_PARSE_FAIL`
- `RETARGET_MERGED_BASE` (`scripts/retarget_merged_base.sh`: `mode=resolve|pr repo= pr= from= to= merged_pr= outcome=retargeted|unchanged reason=`)
- `STANDALONE_AUTO_DECIDE` (`clarify.yml` "Standalone auto-decide": `issue= outcome=answered|skip|failed reason= decisions=`, `reason=delegated_to_clarify_respond` when the worker answers; `orchestrate_clarify_respond.yml` "Standalone RECOMMENDED fallback" and "Record standalone auto-decisions": `issue= outcome=fallback|skip|answered reason=worker_failed|worker_failed_undecided decider= decisions= ad_total= setup_total=`)
- `AI_ENGINE_SELECTED` (`scripts/ai_engine.sh`: `role= engine= model= effort= source=`)
- `AI_ENGINE_FALLBACK` (`scripts/ai_engine.sh`: `role= reason=`; the run uses codex)
- `CLAUDE_POOL` (`scripts/ai_engine.sh` and the sandbox Claude branches: `run role= account= outcome= reason= exit_code=`, `account_skipped account= reason=`)
- `AI_ENGINE_PROJECT_LABEL` (`orchestrate.yml` "Ensure orchestrator labels exist": `label=`, `none` when unset; the label the tracking and wave-1 issues get)
- `AI_ENGINE_PR_LABEL` (`implement.yml` "Create Pull Request": `issue= label=`; the engine label copied from the issue to its PR)
- `SINGLE_ISSUE_SECURITY_PASS` (`scripts/review_single_issue_security_pass.sh`: `mode=gate|status|report pr= head= outcome=clean|hold|dispatched|skip|findings|failed|exhausted reason= cycle=`; clean markers require the authenticated pipeline author and an exact audited PR head. Missing/disabled audits report failed, and an unverifiable marker source holds auto-merge. If result publication fails, report skips review re-dispatch so it cannot run without the marker. `mode=status` writes no GitHub state or step output, but may fetch missing Git history to verify extension ancestry before the review-blocked judge chooses its mode; failed verification reports `unverifiable`. `outcome=hold reason=cycles_exhausted` writes `exhausted=true` only for completed current-head findings, and status also emits `SINGLE_ISSUE_SECURITY_PASS_AUDITED_HEAD`. Without a completed audit the gate retries a bounded number of times per head before reporting `exhausted_unaudited` and holding without the judge bypass. The judge re-verifies the audited head before a security-mode merge. Cycles available = `MAX_SECURITY_PASS_CYCLES` plus one per distinct fix SHA in a trusted `ai:single-issue-security-pass-extension:v1` marker whose commit is reachable from the audited head; duplicate comments for one SHA count once, and a mismatched checkout holds the gate and skips report publication.)
- `RB_JUDGE_SECURITY_PASS` (`scripts/review_rb_judge_security_pass.sh`, sourced by `review_rb_judge.sh`: `mode=detect|findings|merge_gate|extension|severity_block pr= outcome= reason=`; `severity_block` converts merges to fixes while retries remain and holds any final-round action, including `close_and_reissue`, when high/critical/unrated findings remain. For blocking findings the judge withdraws prior auto-merge enrollment even if the live head moved, then refuses to act on a mismatched head; an unreadable enrollment or failed disable stops the judge. Unavailable hold-comment history fails closed to avoid duplicate comments and alerts. `merge_gate outcome=hold` means a judge merge waited for the single-issue security pass and the judge step output `judge_action=security_hold`. A final-retry `fix` is treated as a merge without creating a fix commit only when no blocking findings remain.)
- `ACTIVATION_VERIFY` (`scripts/activation_verify.sh`: `mode=pr|project item= verdict=LIVE|DORMANT code_gaps= operator_gaps= outcome=posted|skip reason=`)
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
- `REISSUE_FILES_TOUCHED_UNION`
- `REISSUE_FILES_TOUCHED_NEW_OUTPUTS`
- `REISSUE_ORCHESTRATOR_METADATA_CARRIED`
- `REISSUE_ORCHESTRATOR_METADATA_ABSENT`
- `FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1`
- `FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1`
- `FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1`
- `FINGERPRINT_STATE_SELFHEAL_V1`
- `FINAL_MERGE_INELIGIBILITY_ALERT_SENT`
- `EAGER_DRAFT_PR_CREATED`
- `APPLY_ANALYSIS_SKIPPED`
- `APPLY_ANALYSIS_DISPATCHED`
- `APPLY_ANALYSIS_CANDIDATES`
- `PROMOTE_CYCLE_SKIPPED`
- `PROMOTE_CYCLE_FAILED`
- `PROMOTE_CYCLE_DISPATCHED`
- `COMPREHENSIVE_VERIFICATION_DISPATCHED`
- `COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED`
- `COMPREHENSIVE_PROMOTION_DISPATCHED`
- `COMPREHENSIVE_PROMOTION_DEFERRED`
- `COMPREHENSIVE_PROMOTION_HOLD`
- `COMPREHENSIVE_PROMOTION_DONE`
- `COMPREHENSIVE_PROMOTION_FAILED`
- `COMPREHENSIVE_PROMOTION_SKIPPED`
- `COMPREHENSIVE_VERIFYING_COMPLETE`
- `COMPREHENSIVE_MARKER_UNTRUSTED`
- `TRACKING_ISSUE_LABEL_APPLIED`
- `TRACKING_ISSUE_BINDING_FAILED`
- `RELEASE_STALE_TIP`
- `RELEASE_UNTESTED_HEAD`
- `TRACKING_ISSUE_COMMENT_POSTED`
- `AUTO_RELEASE_SKIPPED`
- `AUTO_RELEASE_DISPATCHED`
- `IMPLEMENT_BLOCKED_TERMINALIZED`
- `EAGER_DRAFT_PR_PROMOTED`
- `INTEGRATION_STALE_ALERT_SENT`
- `STALL_INFLIGHT_DIRECT_CHECK`
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
- `EDITOR_REVIEWER_CHECKSUM_UNVERIFIED`
- `EDITOR_REVIEWER_CHECKSUM_SUMMARY`
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
- `SECURITY_PASS_FIX_MERGED_EVIDENCE`
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
- `SECURITY_PASS_ADVISORY_FOLLOWUP_DEFERRED`
- `SECURITY_PASS_ADVISORY_FOLLOWUPS_FILED`
- `SECURITY_PASS_AUTO_RESET`
- `SECURITY_PASS_AUTO_RESET_SKIPPED`
- `STAGED_SUPPORT_LATCH_RELEASED`
- `STAGED_SUPPORT_LATCH_SKIP`
- `STAGED_SUPPORT_LATCH_RELEASE_SKIPPED`
- `ORCHESTRATOR_ENGINE_SHA`
- `SECURITY_PASS_ADVISORY_FOLLOWUP_UNBLOCKED`
- `SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED`
- `VALIDATION_RUN_ATTRIBUTION`
- `CI_CANCELLED_RERUN`

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
- `WORKFLOW_HEAL_REPORT`
- `WORKFLOW_HEAL_AUTOFIX_REPORT`
- `WORKFLOW_HEAL_PR_RECONCILE`
- `WORKFLOW_HEAL`
- `WORKFLOW_HEAL_EVIDENCE`
- `HEAL_EVIDENCE_SCOPE_LOCK`
- `EDITOR_GIT_CREDENTIALS`
- `IMPLEMENT_ISOLATION`
- `AUTOFIX_FINGERPRINT`
- `AUTOFIX_FINGERPRINT_CAP_TRIPPED`
- `AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED`
- `AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED`
- `NOOP_RECOVERY_SKIP_FINGERPRINT_CAP`
- `REVIEW_EDITOR_PREFLIGHT`
- `STAGE_MAIN_PINNED_DIVERGENCE`
- `WORKTREE_REGISTER`
- `WORKTREE_DEREGISTER`
- `WORKTREE_GC`
- `WORKTREE_REGISTRY_REBUILD`
- `WORKTREE_REGISTER_INVALID_NAME`
- `WORKTREE_REGISTER_FAIL`
- `WORKTREE_DEREGISTER_FAIL`
- `opencode_agent_failure`
- `AUTOFIX_FAILURE_HEADLINE`
- `MODEL_CATALOG_BACKFILL`
- `CLAUDE_FIXER_AUTO_MERGE`
- `SECURITY_AUDIT_TARGET`

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
LOG_PREFIX.name=LABEL_SYNC_DELETED
LOG_PREFIX.name=LABEL_SYNC_RETIRED_ABSENT
LOG_PREFIX.name=RETIRED_FILE_REMOVED
LOG_PREFIX.name=RETIRED_FILE_KEPT_MODIFIED
LOG_PREFIX.name=AUTOFIX_PEER_CHECK
LOG_PREFIX.name=AUTOFIX_DISPATCH_SKIPPED
LOG_PREFIX.name=AUTOFIX_DISPATCH_ISSUED
LOG_PREFIX.name=AUTOFIX_GATE_SKIP
LOG_PREFIX.name=AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD
LOG_PREFIX.name=AUTOFIX_GATE_TERMINAL_SAME_HEAD_UNCHECKED
LOG_PREFIX.name=AUTOFIX_GATE_TERMINAL_SAME_HEAD_OVERRIDE
LOG_PREFIX.name=AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED
LOG_PREFIX.name=AI_PHASE_FAILURE_V1
LOG_PREFIX.name=AI_PHASE_GATE_V1
LOG_PREFIX.name=WORKFLOW_SCENARIO_TRACE_WRITTEN
LOG_PREFIX.name=WORKFLOW_SCENARIO_TRACE_PARSE_FAIL
LOG_PREFIX.name=RETARGET_MERGED_BASE
LOG_PREFIX.name=STANDALONE_AUTO_DECIDE
LOG_PREFIX.name=AI_ENGINE_SELECTED
LOG_PREFIX.name=AI_ENGINE_FALLBACK
LOG_PREFIX.name=CLAUDE_POOL
LOG_PREFIX.name=AI_ENGINE_PROJECT_LABEL
LOG_PREFIX.name=AI_ENGINE_PR_LABEL
LOG_PREFIX.name=SINGLE_ISSUE_SECURITY_PASS
LOG_PREFIX.name=ACTIVATION_VERIFY
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
LOG_PREFIX.name=REISSUE_FILES_TOUCHED_UNION
LOG_PREFIX.name=REISSUE_FILES_TOUCHED_NEW_OUTPUTS
LOG_PREFIX.name=REISSUE_ORCHESTRATOR_METADATA_CARRIED
LOG_PREFIX.name=REISSUE_ORCHESTRATOR_METADATA_ABSENT
LOG_PREFIX.name=FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1
LOG_PREFIX.name=FINGERPRINT_STATE_SELFHEAL_V1
LOG_PREFIX.name=FINAL_MERGE_INELIGIBILITY_ALERT_SENT
LOG_PREFIX.name=EAGER_DRAFT_PR_CREATED
LOG_PREFIX.name=APPLY_ANALYSIS_SKIPPED
LOG_PREFIX.name=APPLY_ANALYSIS_DISPATCHED
LOG_PREFIX.name=APPLY_ANALYSIS_CANDIDATES
LOG_PREFIX.name=PROMOTE_CYCLE_SKIPPED
LOG_PREFIX.name=PROMOTE_CYCLE_FAILED
LOG_PREFIX.name=PROMOTE_CYCLE_DISPATCHED
LOG_PREFIX.name=COMPREHENSIVE_VERIFICATION_DISPATCHED
LOG_PREFIX.name=COMPREHENSIVE_VERIFICATION_NOT_DISPATCHED
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_DISPATCHED
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_DEFERRED
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_HOLD
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_DONE
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_FAILED
LOG_PREFIX.name=COMPREHENSIVE_PROMOTION_SKIPPED
LOG_PREFIX.name=COMPREHENSIVE_VERIFYING_COMPLETE
LOG_PREFIX.name=COMPREHENSIVE_MARKER_UNTRUSTED
LOG_PREFIX.name=TRACKING_ISSUE_LABEL_APPLIED
LOG_PREFIX.name=TRACKING_ISSUE_BINDING_FAILED
LOG_PREFIX.name=RELEASE_STALE_TIP
LOG_PREFIX.name=RELEASE_UNTESTED_HEAD
LOG_PREFIX.name=TRACKING_ISSUE_COMMENT_POSTED
LOG_PREFIX.name=AUTO_RELEASE_SKIPPED
LOG_PREFIX.name=AUTO_RELEASE_DISPATCHED
LOG_PREFIX.name=IMPLEMENT_BLOCKED_TERMINALIZED
LOG_PREFIX.name=EAGER_DRAFT_PR_PROMOTED
LOG_PREFIX.name=INTEGRATION_STALE_ALERT_SENT
LOG_PREFIX.name=STALL_INFLIGHT_DIRECT_CHECK
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
LOG_PREFIX.name=EDITOR_REVIEWER_CHECKSUM_UNVERIFIED
LOG_PREFIX.name=EDITOR_REVIEWER_CHECKSUM_SUMMARY
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
LOG_PREFIX.name=SECURITY_PASS_FIX_MERGED_EVIDENCE
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
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_FOLLOWUP_DEFERRED
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_FOLLOWUPS_FILED
LOG_PREFIX.name=SECURITY_PASS_AUTO_RESET
LOG_PREFIX.name=SECURITY_PASS_AUTO_RESET_SKIPPED
LOG_PREFIX.name=STAGED_SUPPORT_LATCH_RELEASED
LOG_PREFIX.name=STAGED_SUPPORT_LATCH_SKIP
LOG_PREFIX.name=STAGED_SUPPORT_LATCH_RELEASE_SKIPPED
LOG_PREFIX.name=ORCHESTRATOR_ENGINE_SHA
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_FOLLOWUP_UNBLOCKED
LOG_PREFIX.name=SECURITY_PASS_JUDGE_KEEP_FIXING_CAPPED
LOG_PREFIX.name=VALIDATION_RUN_ATTRIBUTION
LOG_PREFIX.name=CI_CANCELLED_RERUN
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
LOG_PREFIX.name=WORKFLOW_HEAL_REPORT
LOG_PREFIX.name=WORKFLOW_HEAL_AUTOFIX_REPORT
LOG_PREFIX.name=WORKFLOW_HEAL_PR_RECONCILE
LOG_PREFIX.name=WORKFLOW_HEAL
LOG_PREFIX.name=WORKFLOW_HEAL_EVIDENCE
LOG_PREFIX.name=HEAL_EVIDENCE_SCOPE_LOCK
LOG_PREFIX.name=EDITOR_GIT_CREDENTIALS
LOG_PREFIX.name=IMPLEMENT_ISOLATION
LOG_PREFIX.name=AUTOFIX_FINGERPRINT
LOG_PREFIX.name=AUTOFIX_FINGERPRINT_CAP_TRIPPED
LOG_PREFIX.name=AUTOFIX_FINGERPRINT_CAP_ALREADY_APPLIED
LOG_PREFIX.name=AUTOFIX_FINGERPRINT_CAP_QUERY_FAILED
LOG_PREFIX.name=NOOP_RECOVERY_SKIP_FINGERPRINT_CAP
LOG_PREFIX.name=REVIEW_EDITOR_PREFLIGHT
LOG_PREFIX.name=STAGE_MAIN_PINNED_DIVERGENCE
LOG_PREFIX.name=WORKTREE_REGISTER
LOG_PREFIX.name=WORKTREE_DEREGISTER
LOG_PREFIX.name=WORKTREE_GC
LOG_PREFIX.name=WORKTREE_REGISTRY_REBUILD
LOG_PREFIX.name=WORKTREE_REGISTER_INVALID_NAME
LOG_PREFIX.name=WORKTREE_REGISTER_FAIL
LOG_PREFIX.name=WORKTREE_DEREGISTER_FAIL
LOG_PREFIX.name=opencode_agent_failure
LOG_PREFIX.name=MODEL_CATALOG_BACKFILL
LOG_PREFIX.name=AUTOFIX_FAILURE_HEADLINE
LOG_PREFIX.name=CLAUDE_FIXER_AUTO_MERGE
LOG_PREFIX.name=SECURITY_AUDIT_TARGET

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
| claude-pool-broker | worker | `claude-pool-broker` (`https://claude-pool-broker.shubhodeep.workers.dev`, workers.dev only) | FT_GAMES_CF | Claude engine token broker (plan Phase 4). Source `tools/claude-pool-broker/`; deployed by the session with `wrangler deploy` (§24.C). Secret `CLAUDE_POOL_TOKENS` is written only by `shubhodeep1/claude-workers` `claude-pool-key-sync.yml`. |

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
- Review-ledger cache restore/save steps use the run-independent `${RUNNER_TEMP}/review-ledger-cache/<owner>/<repo>/pr-<N>/` staging root and copy to/from the active workspace. The repository/PR scope is part of the cache path-version contract: it must stay byte-identical between both review saves and the validate behavioural-smoke restore while isolating concurrent PRs on persistent self-hosted runners.
- Validate-hints caching uses a separate run-independent `${RUNNER_TEMP}/validate-hints-cache/<owner>/<repo>/<discovery-fingerprint>/` staging root. `validate.yml` stages restored hints into the active workspace before validation and stages generated hints back after `validate_process.sh`, preserving exact-key invalidation without embedding the per-run workspace in the cache version.
- The ≥2-reviewer floor rule is non-overridable at classification time: `scripts/review_floor_rules.sh` promotes same-file, nearby findings from distinct reviewers into `FLOOR_MULTI_REVIEWER`, and those tags remain non-skippable even if the consolidator down-ranks the issue.
- The review-autofix reviewer pass remains model-diversity-first. The consolidator's seven lenses are this repo's equivalent of Cloudflare's seven specialised review sub-agents; the pipeline does not run one fixed model per lens.
- Additive Phase M note: `prompts/review-consolidator.txt` now appends an eighth `DOCS COVERAGE (DIATAXIS)` lens after those original seven. The first seven lens names and order stay byte-for-byte stable; the new lens is advisory-only (`SEVERITY: low`, normally `CLASSIFICATION: nice-to-have`), is grounded in reviewer evidence plus touched files for user-visible changes, and names only still-missing `Reference` / `How-to` / `Tutorial` / `Explanation` updates (or `Docs coverage: complete` when already covered).
- Reviewer prompts now carry explicit anti-rules in both `prompts/review-reviewer-checklist.txt` (`WHAT NOT TO FLAG` under each lens) and the shared `COMMON ANTI-RULES` block rendered by `scripts/review_run_reviewers.sh`.
- `scripts/review_run_reviewers.sh` also carries the `lite | standard | full` review-tier resolver, on by default (`REVIEW_TIER_RESOLVER_ENABLED=true`). `lite` (1 reviewer) is any diff of at most `REVIEW_TIER_LITE_MAX_LOC` lines that touches no protected path; `standard` (4 reviewers) is any diff of at most `REVIEW_TIER_STANDARD_MAX_LOC` lines in any folder, including small protected diffs; `full` is everything larger plus the force-review and fail-open tier. Protected paths are the deterministic skip gate's list (`PROTECTED_SKIP_SUPPRESSED` in `review_autofix.yml`), checked on both sides of renames; `tests/test_review_autofix_review_pipeline_contract.py` keeps the two lists identical. `standard` runs the `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` list, by default four panel models (`minimax/minimax-m3,deepseek/deepseek-v4-pro,qwen/qwen3.7-plus,openai/gpt-6-luna`), so `google/gemini-3.1-flash-lite` and `z-ai/glm-5.2` run only on the full panel. With `REVIEW_TIER_LITE_REVIEWER_SLUG` empty (the default), `lite` draws its reviewer from that standard list (from `REVIEWER_MODELS` when the list is empty or names a slug not on the panel) by the lowest `sha256("<PR number>:<model>")`, so a PR keeps the same reviewer across rounds and reruns; an empty standard list draws four reviewers from `REVIEWER_MODELS` the same way. Set either variable to pin reviewers. `AUTOFIX_SKIP_*` fast paths stay authoritative, `[force-review]` / `force-review` still force full review, a full panel forced by the risk-tier resolver below (`REVIEWER_RISK_TIER_FORCED_FULL`) is kept (`reason=risk_tier_forced_full`), a random pick that returns too few reviewers fails open to the full panel (`reason=random_reviewer_pick_failed`), and `lite` reuses `REVIEW_CONSOLIDATOR_ENABLED=0` to skip the consolidator.
- `scripts/review_run_reviewers.sh` can classify a PR into `trivial | lite | full` reviewer tiers from reviewer-visible diff LOC/file counts, with `REVIEWER_RISK_TIER_ALWAYS_FULL_REGEX` forcing `full` on sensitive paths. Default tier fan-out follows the live `REVIEWER_MODELS` order from `.github/workflows/review_autofix.yml`: trivial = first reviewer, lite = first two reviewers, full = the complete configured set.
- `scripts/review_filter_uninteresting_files.sh` strips low-signal lock/generated/minified paths before reviewer fan-out and emits `REVIEWER_FILTER_SKIP: <path> <reason>` for each skipped file. Default exemptions remain `db/contracts/**`, `**/migrations/**`, and `**/migrate/**`.
- `.github/workflows/review_autofix.yml` now runs a fail-open local slop-scan preflight (gated by `SLOP_SCAN_ENABLED`, default `true`) on PR-changed `scripts/*.py`, `scripts/*.sh`, and `validation/**/*.sh` Python heredocs. It writes `.ai/slop_scan/findings.json`, feeds that JSON to reviewer and consolidator prompts as advisory untrusted context, and removes the runtime artifact before commit-producing steps so it cannot leak into staged changes.
- Consumer-repo review commits snapshot untracked paths before the editor runs in `PRE_EDITOR_UNTRACKED_FILE`. `scripts/review_commit_changes.sh` removes paths that were already untracked plus pipeline-owned artifacts, records removals in `REVIEW_REMOVED_NEW_FILES_FILE`, and preserves other editor-created files for the existing staging and write-guard path; a missing snapshot retains the legacy delete-all fallback.
- `scripts/review_agents_md_materiality.sh` is deterministic-path-glob v1: it writes a JSON result payload plus a non-blocking PR comment headed `## AI Materiality Advisory` when materiality is `high` or `medium` and root `agents.md` is unchanged. `AGENTS_MD_MATERIALITY_LLM_FALLBACK_ENABLED` is reserved only; enabling it still does not trigger a model call in the current shipped script.
- When `REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED=true`, `scripts/review_consolidate.sh` feeds that helper JSON into the consolidator prompt as advisory untrusted context. This is the Lens 7 companion to the separate advisory comment path controlled by `AGENTS_MD_MATERIALITY_ENABLED`. Lens 7 (`NAMING / BACKWARD COMPATIBILITY`) may then emit a default-`high` `AGENTS.md materiality` finding when operator-visible structural changes leave root `agents.md` unchanged, but downgrades or omits it when equivalent touched docs already cover the behavior.
- The deterministic review skip requires a complete paginated `/pulls/{n}/files` list for both small-diff and doc-only candidates, matched against the existing PR-details `changed_files` count. Missing/malformed/empty/partial responses and GitHub's 3,000-file ceiling route to review. Both names of a rename are checked; nested or case-variant agent instructions and automation paths (`.github/`, `.claude/`, `scripts/`, `prompts/`, `workflow-templates/`, `validation/`, `ai-memory/`, `db/contracts/`) plus root build/dependency/lint config suppress skip independently of `AGENTS_MD_MATERIALITY_ENABLED`. Benign docs and small code changes still qualify when evidence is complete; the head-bound merge check remains in place.
- `REVIEW_LEDGER_REREVIEW_ENABLED` gates consolidator-side suppression of repeated `accepted-residual` / `won't-fix` findings from the existing review ledger and the review-blocked judge's ledger-fed prior-round decision input. `scripts/review_rb_judge.sh` renders that `=== BEGIN PRIOR ROUND DECISIONS ===` block via `render_review_rb_prior_round_decisions_file`, and `prompts/mode-judge-review-blocked.txt` treats it as advisory history rather than fresh reviewer evidence.
- `REVIEWER_CIRCUIT_BREAKER_ENABLED` persists reviewer health under `.ai/review_runtime/pr-<PR>/reviewer_health_state.json`. Retryable reviewer failures first retry with cheaper reasoning, then consult `scripts/reviewer_failback_chains.json`; unmapped reviewers fail open via `REVIEWER_FAILBACK_UNMAPPED`. The live-roster mapping file covers `deepseek/deepseek-v4-pro -> deepseek/deepseek-v3.2`, `google/gemini-3.8-flash -> google/gemini-3.1-flash-lite`, `minimax/minimax-m3 -> minimax/minimax-m2.5`, `openai/gpt-6-luna -> openai/gpt-5.6-luna`, `qwen/qwen3.7-plus -> qwen/qwen3.6-plus`, and `z-ai/glm-5.2 -> z-ai/glm-5.3-flashx`; the Gemini, GPT, Qwen, and GLM failback targets keep 1M+ token windows because reviewer prompts regularly exceed 250K tokens, while the DeepSeek (`deepseek/deepseek-v3.2`, 128K) and MiniMax (`minimax/minimax-m2.5`, 200K) targets have smaller windows than the largest reviewer prompts. It also retains retired-roster / operator-override mappings `google/gemini-3.1-flash-lite -> google/gemini-3-flash-preview`, `moonshotai/kimi-k3 -> moonshotai/kimi-k2.7-code`, `qwen/qwen3.6-plus -> qwen/qwen3-coder-plus`, `x-ai/grok-4.20 -> x-ai/grok-4.3`, and `x-ai/grok-4.6 -> x-ai/grok-4.20` (the former `x-ai/grok-4.20 -> x-ai/grok-4.1-fast` entry was dropped because OpenRouter no longer serves that slug). Every live reviewer is mapped; `REVIEWER_FAILBACK_UNMAPPED` still governs any operator-supplied slug without a chain entry.
- Reviewer loop guards (`scripts/review_run_reviewers.sh` watchdog, review panel only; the judge, consolidator, consensus summariser, and smoke reviewer-role callers are not capped). Every 10 s poll reads the attempt's OpenCode `--format json` event stream. `REVIEWER_MAX_STEPS` (default `120`): once an attempt starts more than that many turns (`step_start` events), the watchdog kills it (`wd_reason=max_steps`, log line `killed by watchdog ... (turn limit N ...); not retried.`) and the slot fails as a non-retryable failure with no cheaper-reasoning retry and no failback; a fast loop may overshoot by the turns that start within one poll. `REVIEWER_TOOL_REPEAT_LIMIT` (default `10`, minimum `2`): once the last N completed tool calls are identical (same tool and same JSON input, so paged `read` calls with different offsets never match), the watchdog kills the attempt (`wd_reason=tool_repeat`) and it follows the normal retryable path (class `tool_repeat`: cheaper reasoning, then failback). Invalid values fall back to the defaults with a `::warning::`. OpenCode's own agent `steps` setting is not used: in 1.18.23 it only injects a "maximum steps reached" instruction and keeps offering tools, so a looping model continues. Background: `x-ai/grok-4.20` reviewer passes looped on one repeated tool call for 2,205 / 1,468 / 234 turns (runs 35949371968, 36483245451, 36522631293) while real passes peaked at 101 turns; it left the default roster on 2026-09-29.
- `scripts/cost_audit.py` now parses additive review telemetry fields `cache_hit_rate`, `wall_clock_p50_ms`, `wall_clock_p99_ms`, `break_glass_count`, and `context_budget_warn_count`. `CONTEXT_BUDGET_WARN` is emitted pre-flight from review / consolidator / judge paths when a prompt exceeds the configured per-model context threshold.
- `scripts/codex_heartbeat.sh` wraps long-running `codex exec` calls in reviewer, consolidator, review-blocked judge, conflict-resolver, and validate/self-heal paths, emitting `CODEX_HEARTBEAT: phase=<phase> elapsed_secs=<n>` during silent periods.
- `REVIEW_APPROVAL_RUBRIC_ENABLED` lets the review-blocked judge emit logical `review_state` values (`APPROVE`, `APPROVE_WITH_COMMENTS`, `COMMENT`, `REQUEST_CHANGES`) that `scripts/post_review_comment.sh --review-state` maps to outbound PR reviews. With `REVIEW_BREAK_GLASS_ENABLED`, a human comment anchored as `@codex break-glass` downgrades only the outbound `REQUEST_CHANGES` event to comment-only and logs `BREAK_GLASS`, while preserving the judge's written review body.
- Every `gh pr merge` call owned by `review_autofix.yml` is bound with `--match-head-commit` to the head that authorized it. Deterministic skips use the gate's `head_sha`, normal review paths use the checked-out `INITIAL_HEAD_SHA` captured before read-only/fork exits, and `scripts/review_rb_judge.sh` uses the checked-out `RB_JUDGED_HEAD_SHA` embedded in the judge prompt. Unknown or moved heads fail closed, and linked issues do not advance to `ai:ready-to-merge` after a rejected bound merge.
- **CI job layout (#4707).** `.github/workflows/ci.yml` runs as parallel jobs, not one sequential job: `static-checks` (the shell-block guard, YAML lint, drift check, actionlint, Python syntax and ruff, schema and prompt checks, ShellCheck; budget 15 minutes), four test jobs that each take a contiguous slice of the old step order (`tests-hooks-and-orchestrator`, `tests-heal-plan-and-validation`, `tests-promote-stall-and-review`, `tests-release-and-log-analysis`; 20 minutes each), and the `orchestrate-poll` matrix (4 groups, 20 minutes each). The final job keeps the id `lint`, so the aggregate status is still `CI / lint`: it needs every other job, runs with `if: always()`, and fails unless every needed job's result is `success`. Without `always()` it would be skipped when a job fails, and GitHub counts a skipped required check as passing. The old single job took 40–45 minutes (its budget went from 45 to 60 minutes in #4706 as a stopgap); the first run of the split layout (run 36523765261, 2026-09-29) finished in 9.0 minutes, with `orchestrate-poll (0)` as the critical path at 8.7 minutes (it also carries the fast-fail subset and the three single-file modules) and `tests-promote-stall-and-review` the slowest test job at 8.2 minutes. `tests/test_ci_job_split_contract.py` pins the aggregate's `needs` list and failure behaviour, the budgets, and that no step runs in two jobs. Add a new CI step to one existing job; a new job must also be added to `lint`'s `needs`.
- The `orchestrate-poll` matrix runs `tests/test_orchestrate_poll_process.py`, CI's largest module: most of its tests spawn the real poller as a bash subprocess inside a throwaway sandbox, costing seconds each rather than milliseconds, and run sequentially it took roughly 35 minutes on a 4-core box. Each test allocates its own tempdir sandbox in `_make_poller_sandbox`, so the module shards with no shared state. The split has two levels, both `NR % total == n`: each matrix group takes its slice of the post-fast-fail subset (group index and count from `strategy.job-index` / `strategy.job-total`, exported as `CI_POLL_TEST_GROUP_INDEX` / `CI_POLL_TEST_GROUP_COUNT`), then shards that slice across `CI_POLL_TEST_SHARDS` local workers (default 4). A group index outside the matrix fails the step instead of silently skipping tests. The fast-fail subset and the three single-file modules (`test_orchestrate_poll_noop_suspicious_recovery.py`, `test_state_snapshot.py`, `test_run_substate_ledger.py`) run in group 0 only. `tests/test_ci_poll_test_sharding.py` verifies that both levels and their composition are a true partition. A failing shard fails its group, and a shard whose exit code was never recorded counts as failed rather than passing silently. The release gates carry the same sharded step in one runner: the `validate-scripts` job in `mark-stable.yml` and `test-and-mark-stable.yml` shards the full module (no fast-fail subset, no groups) under the same `CI_POLL_TEST_SHARDS` var. Its budget is 60 minutes since #4707, because that job measured 37 minutes on test-and-mark-stable run 36374918973. It was 45 minutes after release v1.27.0 (run 33073743283) was lost to the serial module overrunning that job's previous 30-minute cap, where the cancelled job skipped `validate`/`release`. `tests/test_ci_poll_test_sharding.py` pins the ported step's partition expression, failure handling, and budget in both workflows.
- `scripts/review_resolve_review_threads.sh` closes the loop on PR review comments. The pipeline has always *read* them — `scripts/review_collect_pr_metadata.sh` fetches `issues/<pr>/comments` and `pulls/<pr>/comments` into `PR_ALL_COMMENTS_CONTEXT_FILE`, which both `review_run_reviewers.sh` and `review_apply_fixes.sh` inline, and the editor must audit each one under `PR comment audit:` — but nothing marked the thread resolved, so a fixed comment looked identical to an unread one. The `Resolve addressed PR review threads` step in `review_autofix.yml` now runs after the editor summary and its persisted-change/no-op safety checks, then resolves each validated audited thread; an `applied` disposition additionally requires a productive commit. Mapping is keyed on the comment **id** carried by the audited `entry[N]`, resolved through `PR_ALL_COMMENTS_CONTEXT_FILE`, never on a path/line pair: an entry the editor never listed, an index whose audited path disagrees with the real comment's path, a non-`review_comment` kind, and an already-resolved thread are all skipped. That is what keeps two contradictory comments at one file:line from resolving each other. `ignored` entries are resolved too, but only after the editor's stated reason is posted as a thread reply, so the reviewer sees the disagreement and can reopen. Thread lookup is one paginated GraphQL query per run (§15); GraphQL is used because REST exposes no resolve-review-thread endpoint, and the §21.D/§23.D REST preference addresses the Claude Code Web proxy in interactive sessions, not this Actions-side caller. Every failure path warns and exits 0.
- `.github/workflows/review_autofix_sweep.yml` skips a PR whose head ref already has a `queued` or `in_progress` review run, so a 30-minute tick cannot stomp a synchronize-fired run mid-edit. That guard now distinguishes the two states. `in_progress` suppresses indefinitely — the codex-agent job legitimately runs over an hour. `queued` suppresses only until `SWEEP_STALE_QUEUED_MINUTES` (default 120, above the ~94-minute longest observed legitimate concurrency wait), because GitHub can wedge a run in `queued` with zero jobs and then reject both `cancel` (409 `Cannot cancel a workflow run that has not been queued yet`) and `rerun` (403 `This workflow is already running`). With no cutoff such a run suppressed the sweep forever, and the sweep is the PR's only recovery path, so the guard deadlocked the mechanism it protects — PR #3841 sat unreviewed for 11+ hours behind run `32984498460`. Discounted runs are logged as `AUTOFIX_SWEEP_STALE_QUEUED`, never dropped silently; a run with a missing or unparseable `created_at` still counts as active, and `SWEEP_STALE_QUEUED_MINUTES=0` restores the previous behaviour exactly. The guard also counts `pending` runs — a duplicate dispatch held back by `review_autofix.yml`'s `cancel-in-progress: false` concurrency group reports `pending`, not `queued`, and the same is true for the poller's `_has_active_autofix_run` guard in `scripts/orchestrate_poll_process.sh`. `pending` suppresses indefinitely, like `in_progress` (it is bounded by the running peer's 240-minute job timeout), and is never subject to the stale-queued cutoff. Every review dispatch runs from the default branch and passes only a validated PR number: the sweep since issue #4618, and the poller's `_dispatch_review_for_conflicts`, the merge train's `_mt_dispatch_review` (`scripts/review_merge_train.sh`), and `forward-merge-stable-to-main.yml`'s fallback-PR dispatch since issue #4701. A `--ref <PR head branch>` dispatch ran the unmerged branch's copy of the review workflow with `secrets: inherit` and write permissions. The forward-merge branch is cut from `stable` by the workflow itself, but it moved too, because any writer can push to it before the dispatch. A default-branch run's head branch is the default branch, so the guards find it by name instead: `internal-review.yml` names every `workflow_dispatch` run `Internal: AI Review & Autofix [pr:<N>]`, and the consumer template `workflow-templates/ai-review.yml` names it `AI Review [pr:<N>]` (`run-name`; every other event keeps GitHub's default name). The sweep's snapshot keys a `workflow_dispatch` run with the internal name by `pr:<N>`, and keeps such a run even when GitHub reports its `head_branch` as null (issue #4928); a run with neither a head branch nor that name is dropped. In the poller, `_pr_named_review_dispatch_runs <pr>` lists only the two wrappers' own `workflow_dispatch` runs (`GET actions/workflows/<internal-review.yml | ai-review.yml>/runs?event=workflow_dispatch&created=>=<now − REVIEW_RUN_MAX_RUNTIME_MINUTES>&per_page=100`), page by page until it has read every run the listing's `total_count` reports, at most 10 pages per wrapper (GitHub serves at most 1,000 results for a filtered listing). A wrapper the repo does not have answers 404 and counts as complete and empty; in coding-workflows that is 3 calls (two `internal-review.yml` pages and one `ai-review.yml` 404). It returns 1 when the listing is incomplete: a page failed, a page was malformed, the listing shifted under it, or more runs exist than 10 pages hold. That case is logged as `PR_NAMED_REVIEW_RUNS pr=<N> outcome=incomplete reason=<…>`. An incomplete listing never authorises a dispatch or an empty-commit push, and the next poll cycle retries. Issue #4927 replaced the earlier single `gh run list --event workflow_dispatch --limit 100` page of every workflow's dispatches, which failed open to `[]`. That page covered under 3.5 hours here (153 `internal-review.yml` dispatches in 5 hours, 2026-09-29), so a burst of unrelated dispatches could push a live review run off it and let the stall recovery push an empty commit under it. `_has_active_autofix_run` uses it only when its head-branch lookups found nothing, and counts an incomplete listing as an active run (the dispatch is skipped). The stall-recovery failed-autofix redispatch uses it only when the head-branch lookups found no failed run, and acts on a PR-named failure only when it is newer than every completed head-branch run; on an incomplete listing it neither redispatches nor pushes that cycle. It passes the helper's optional second argument, a lookback of `REVIEW_RUN_MAX_RUNTIME_MINUTES + STALL_THRESHOLD_MINUTES` (370 minutes by default, 4 calls here), because a run that failed at the codex-agent job's 240-minute timeout would leave the 250-minute window minutes after it ended; the in-flight guards keep the default. `_direct_inflight_review_run_on_branch <branch> [pr]` uses it only when its branch listing matched no fresh run, and prints the sentinel `listing-incomplete` (`STALL_INFLIGHT_DIRECT_CHECK … outcome=pr_named_listing_incomplete`) on an incomplete listing, which both empty-commit push sites treat as a skip. The stall judge's `workflow_outcomes` and both empty-commit push guards' cached scans match the names in the cached `actions/runs` blob, and the merge-train release keys active runs as `pr:<N>` from the listing it already makes, so those add no API call. `build_active_issue_set` still maps runs to issues by head-branch pattern. `review_autofix.yml`, the last dispatch candidate after both wrappers, has no PR run name. A consumer repo sees its dispatched runs by name only after its `ai-review.yml` sync. Before run names, the PR #3895 incident (2026-08-29) accumulated 10 duplicate dispatches and 6+ duplicate Telegram conflict warnings in ~95 minutes while the one real resolver — dispatched on `main`'s ref, invisible to both guards — ran to success.
- `AUTOFIX_SKIP_TERMINAL_SAME_HEAD` defaults to `true` and lets the reusable review gate stop `workflow_dispatch` reruns after the newest trusted `REVIEW_AUTOFIX_PARTIAL_V1` marker for the current head sets `resume_should_continue=false`. Reusable workflows retain the caller's `github` context, so sweep dispatches through `internal-review.yml` still expose `github.event_name=workflow_dispatch`; pull-request events remain ineligible. The gate resolves the account authenticated through `GH_PAT` with one `/user` read, accepts marker comments only from that account, and then reads the paginated PR comments. Identity, comment, or parser failures emit `AUTOFIX_GATE_TERMINAL_SAME_HEAD_QUERY_FAILED` and fail open; rejected current-head markers are counted in `AUTOFIX_GATE_NO_SKIP_TERMINAL_SAME_HEAD`. A conflicted PR (`mergeable` not `true`) is never skipped, and inside `codex-agent` a terminal same-head resume (`AUTOFIX_RESUME_TERMINAL=true`) still runs `Detect merge conflicts`, the resolver steps, and `Push all pending commits` for a resolved merge; the editor and `Commit changes` keep skipping that path.
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
| `REVIEW_CONSOLIDATOR_MODEL` | `openai/gpt-6-sol` | Default consolidator model in `review_autofix.yml`. |
| `REVIEW_CONSOLIDATOR_REASONING` | `high` | Default consolidator reasoning effort. |
| `REVIEW_CONSOLIDATOR_TIMEOUT_SECS` | `300` | Default consolidator timeout in seconds. |
| `REVIEW_CONSOLIDATOR_MAX_TOKENS_OUT` | `16000` | Default consolidator output-token budget. |
| `REVIEW_PARSER_FAILOPEN` | `1` | Keep parser failures advisory instead of fatal. |
| `REVIEW_LEDGER_ENABLED` | `1` | Enable per-PR ledger persistence and `ledger_status.txt` emission. |
| `REVIEW_LEDGER_PERSIST_LIMIT` | `2` | Threshold for the `accepted-residual` transition. |
| `REVIEW_LEDGER_PATH` | `.ai/review_issue_ledger/pr-${PR_NUMBER}.txt` | Default per-PR ledger path, persisted through the repository/PR-scoped run-independent cache staging root. |
| `REVIEW_REVIEWER_CHECKLIST_ENABLED` | `1` | Append the reviewer checklist block when the prompt template is available. |
| `REVIEW_REVIEWER_ITERATION_SCOPING` | `1` | Scope later reviewer passes from last-run changed files plus actionable ledger rows; first pass stays full-diff. |
| `REVIEW_LEDGER_REREVIEW_ENABLED` | `false` | Enable ledger-aware re-review suppression in the consolidator and the review-blocked judge's prior-round-decision input. |
| `REVIEW_APPROVAL_RUBRIC_ENABLED` | `false` | Enable logical review-state output from the review-blocked judge and outbound PR-review mapping through `post_review_comment.sh --review-state`. |
| `REVIEW_BREAK_GLASS_ENABLED` | `false` | Enable the anchored `@codex break-glass` override scan; when active it downgrades only the outbound `REQUEST_CHANGES` event to comment-only. |
| `CI_POLL_TEST_SHARDS` | `4` | Parallel local shards for the orchestrate-poll module in each group of CI's `orchestrate-poll` matrix and in the release gates' `validate-scripts` job. `1` is sequential; invalid values warn and fall back to `1`. |
| `CONFLICT_MANIFEST_UNION_ENABLED` | `true` | Deterministically resolve two-sided `.ai/.workspace_source_manifest.txt` content conflicts before the model resolver; manifest-only conflicts are committed as `[ai-merge-resolve]` and skip the model. Integration-sync branches and delete/modify conflicts remain model-resolved. |
| `REVIEW_RESOLVE_THREADS_ENABLED` | `true` | Resolve PR review threads the editor audited in its `PR comment audit:` section. Keyed on comment id, so two comments at one path cannot resolve each other; `ignored` entries get the editor's reason as a reply before resolving. |
| `REVIEW_RESOLVE_THREADS_MAX` | `50` | Per-run cap on resolved review threads; anything above it is warned about and left open. |
| `SWEEP_STALE_QUEUED_MINUTES` | `120` | Age past which a still-`queued` review run stops suppressing a sweep dispatch (wedged-run recovery). `in_progress` runs are never discounted; `0` disables the cutoff. |
| `CLAUDE_BRANCH_PUSH_PR_GRACE_SECONDS` | `300` | `internal-review.yml` push route only: when a `claude/**` push finds no open PR, re-check every 60s for up to this many seconds and skip the no-PR reviewer run once a PR appears (its `pull_request` run reviews the same commit). `0` restores the single lookup; values outside 0-3600 fall back to `300` with a warning; a failed lookup counts as no PR and the review runs when the window ends. Logs `RESOLVE_CLAUDE_BRANCH_PR_WAIT` / `RESOLVE_CLAUDE_BRANCH_PR_LOOKUP_FAILED` (the latter with gh's error text as `error="..."`: one line, at most 200 characters). |
| `REVIEW_TIER_RESOLVER_ENABLED` | `true` | Size-based `lite \| standard \| full` review tiers (1, 4, or all reviewers). Set to `false` to turn them off: the full panel then runs unless `REVIEWER_RISK_TIER_ENABLED` is also on, whose selection (which can be smaller) then stands. |
| `REVIEW_TIER_LITE_MAX_LOC` | `50` | Maximum total diff LOC for the one-reviewer `lite` tier. Any file type qualifies unless the diff touches a protected path, which goes to `standard`. |
| `REVIEW_TIER_LITE_REVIEWER_SLUG` | empty | Empty draws one reviewer from the `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` list (from `REVIEWER_MODELS` when that list is empty or names a slug not on the panel), seeded by the PR number; a set slug pins it. Unknown or unavailable slugs fail open to `full`. |
| `REVIEW_TIER_STANDARD_MAX_LOC` | `200` | Maximum total diff LOC for the four-reviewer `standard` tier, in any folder. |
| `REVIEW_TIER_STANDARD_REVIEWER_SLUGS` | `minimax/minimax-m3,deepseek/deepseek-v4-pro,qwen/qwen3.7-plus,openai/gpt-6-luna` | The `standard` tier's reviewers and the unpinned `lite` pool: four panel models, leaving `google/gemini-3.1-flash-lite` and `z-ai/glm-5.2` to the full panel. An empty value reaching the script (an empty repo variable falls back to this default) draws four reviewers from `REVIEWER_MODELS`, seeded by the PR number. Unknown or unavailable slugs fail open to `full`. |
| `REVIEWER_MAX_STEPS` | `120` | Hard turn cap per review-panel reviewer attempt, enforced by the `scripts/review_run_reviewers.sh` watchdog from OpenCode `step_start` events. An attempt that starts more turns is killed and the slot fails without a retry or failback. Invalid values fall back to `120` with a warning. |
| `REVIEWER_TOOL_REPEAT_LIMIT` | `10` | Consecutive identical tool calls (same tool and same input) that end a review-panel reviewer attempt as a retryable `tool_repeat` failure (cheaper reasoning, then failback). Minimum `2`; invalid values fall back to `10` with a warning. |
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
| `AGENTS_MD_MATERIALITY_MODEL` | `openai/gpt-6-luna` | Reserved future materiality fallback model slug. |
| `AGENTS_MD_MATERIALITY_REASONING` | `medium` | Reserved future materiality fallback reasoning effort. |
| `CONTEXT_BUDGET_WARN_RATIO` | `0.7` | Per-model context-window ratio above which review-surface prompt builders emit `CONTEXT_BUDGET_WARN`. |
| `MAX_PROMPT_TOKENS_FOR_PHASE` | `(empty)` | Absolute prompt-token override that takes precedence over `CONTEXT_BUDGET_WARN_RATIO`; phase-specific `MAX_PROMPT_TOKENS_FOR_<PHASE>` overrides remain supported. |
| `CODEX_HEARTBEAT_ENABLED` | `1` | Enable the `codex_heartbeat.sh` wrapper on long-running review / validate Codex calls. |
| `CODEX_HEARTBEAT_INTERVAL_SECS` | `30` | Silence interval (seconds) between emitted `CODEX_HEARTBEAT` lines. |
| `REVIEW_DIATAXIS_LENS_ENABLED` | `true` | Documentation-only contract row for the advisory `DOCS COVERAGE (DIATAXIS)` consolidator lens. Current branch behavior is prompt-defined only (no separate workflow toggle yet): keep it `low` severity and name only still-missing `Reference` / `How-to` / `Tutorial` / `Explanation` updates. |
| `REVIEW_AGENTS_MD_MATERIALITY_CHECK_ENABLED` | `true` | Enable the consolidator-side companion `AGENTS.md` materiality finding. Unlike `AGENTS_MD_MATERIALITY_ENABLED`, which controls the separate advisory comment helper, this flag only controls whether `review_consolidate.sh` passes the helper JSON into Lens 7 (`NAMING / BACKWARD COMPATIBILITY`). |
| `ENABLE_SECURITY_PASS` | `true` | Enable the scheduled poller's mandatory current-integration-head security gate before validation or finalization. Set to `false` for the immediate operator kill switch and legacy completion behavior. |
| `MAX_SECURITY_PASS_CYCLES` | `5` | Maximum completed consolidated security-fix cycles before persistent findings terminalize as `ai:security-pass-failed`. Resets to `0` when an advancing integration head invalidates a recorded clean pass. Re-audits after a merged fix are delta audits, so the budget bounds persisting findings rather than fresh samples of unchanged code. For standalone PRs, a completed current-head audit is required to enter judge exhaustion mode. |
| `SINGLE_ISSUE_SECURITY_PASS_ENABLED` | `true` | When enabled, hold eligible standalone PRs into the default branch until a security audit of the current head is clean. At the cycle cap an unaudited head gets bounded retries, then remains held without judge exhaustion mode; a completed findings audit for the same head still qualifies if a later attempt fails. A missing or unwritable `GITHUB_OUTPUT` fails the gate step closed; dispatch failure at any cycle holds the merge for a later review retry. Disabling the flag restores the pre-pass review-gate and deterministic-skip merge behavior. Only a sole verified automation follow-up is exempt; see `README.md` for dispatch and failure modes. |
| `SECURITY_PASS_EXHAUSTED_HEAD_AUDIT_ATTEMPTS` | `2` | Maximum trusted pending audit attempts per head with a cycle number above the standalone pass's cycle cap. Pre-cap pending markers do not consume these extra attempts. Invalid or non-positive values fall back to `2`; a failed exhausted retry dispatch holds the merge. |
| `SECURITY_PASS_PENDING_STALE_HOURS` | `6` | A pending single-issue audit holds auto-merge until its marker is this many hours old; the next review run re-dispatches. Invalid or non-positive values fall back to `6`. Only a sole verified automation-linked issue skips the pass; multiple linked issues are audited. When `GH_PAT` is absent, both gate and reporter trust only `github-actions[bot]` markers. |
| `MAX_SECURITY_PASS_FIX_REISSUES` | `2` | Maximum re-issues of one `ai:implementation-failed` consolidated security-fix issue per fix cycle before the pass terminalizes as `ai:security-pass-failed`. |
| `SECURITY_PASS_CONFIDENCE_GATE` | `8` | Minimum 1-10 confidence score for findings that block the project security pass. |

## Integration-sync verifier + bootstrap contract

- Source-repo resolver attempts in `scripts/review_conflict_resolve.sh` snapshot the
  worktree and merge index before invoking the model. An out-of-scope edit gets
  a bounded scope-feedback retry only after the pre-attempt state is restored
  and verified; an unsafe restore fails closed. Consumer repos retain the
  previous path, and the final `check_resolver_diff.sh` commit gate is unchanged.
  The model itself runs on a private copy of the captured merge index
  (`GIT_INDEX_FILE=${RUNTIME_DIR}/resolver_model_index`, refreshed before every
  attempt by `_resolver_model_index_prepare`), so a `git add` of the file it
  resolved no longer changes the real index that both scope guards require to
  stay unchanged (#5627). The script stages the accepted resolution itself; a
  model that bypasses the copy still fails closed. Because OpenCode's snapshot
  tracking runs git with the inherited environment, the resolver's own OpenCode
  config sets `snapshot: false`. Both changes apply to the source repo only.

- `scripts/verify_integration_fingerprints.py` supports `--baseline-fingerprints-state <out>` / `--compare-against-baseline <in>` alongside `--ref`; capture mode records ref-accurate `head_sha` metadata, compare mode emits `PRE_EXISTING_FINGERPRINT_DRIFT_V1` markers for pre-existing drift that should not block the resolver commit, and the verifier-side false-positive defenses emit `FINGERPRINT_PARTIAL_REMOVAL_FALSE_POSITIVE_V1` (capture-side multi-occurrence partial removal), `FINGERPRINT_POST_CAPTURE_EVOLUTION_FALSE_POSITIVE_V1` (a `must_contain` line modified after capture by a non-`[ai-merge-resolve]` commit), and `FINGERPRINT_POST_CAPTURE_REINTRODUCTION_FALSE_POSITIVE_V1` (a `must_not_contain` line re-added after capture by a non-`[ai-merge-resolve]` commit — e.g. a back-merge of the default branch keeping its still-present copy) when the ref-mode wave-dispatch gate suppresses a non-resolver false positive. The two post-capture defenses share one direction-agnostic pickaxe primitive and both fail closed in working-tree mode, so the resolver's own pre-commit self-check stays strict and still cannot silently revert merged intent.
- `.github/workflows/review_autofix.yml` stages required and main-primary helpers from the verified reusable-workflow SHA; PR-head copies are review data, not runtime code. `render_prompt.py`, `review_conflict_resolve.sh` and their dependencies ship with that same workflow commit. Embedded PR-diff template syntax is still handled by `render_prompt.sh` with `RENDER_PROMPT_SKIP_SYNTAX_VALIDATION=1` after assembly, while static templates retain strict validation. Optional support missing from that commit skips the feature; required support fails closed. The model catalog comes from the same commit as the reviewer roster, never from a PR branch or a separately resolved main snapshot.
- `scripts/review_merge_train.sh` is staged through `REQUIRED_BOOTSTRAP_SCRIPTS` for `review_autofix.yml` and copied best-effort (with `label_helpers.sh`) next to `gh_helpers.sh` by `orchestrate_poll.yml` and `cancel_on_pr_close.yml`, which do not run the full support staging. Both callers treat a missing copy as "skip this tick" so an older `SCRIPT_REF` keeps polling; its own API budget is documented in the script header (CLAUDE.md §15).
- `scripts/review_conflict_resolve.sh` persists one `AUTOFIX_RESOLVER_RETRY_STATE_V1` PR-body block per final PR/head SHA, keyed by normalized fingerprint failure signature. `RESOLVER_ESCAPE_THRESHOLD_N` is the per-tier same-head, same-signature step size: multiples advance `strict` → `ratio` → `count_only` → `warn_only`, emit `FINGERPRINT_TIER_DOWNGRADED_V1`, and after the next multiple the script labels the **final PR issue** `ai:resolver-escalated` and records `escalated_at` for poller-side suppression / branch-rebuild gating.
- Conflict completion is a trusted-runner Git-index invariant. `scripts/review_conflict_prepare.sh` records initially unmerged paths separately from fingerprint-only resolver expansions; after the isolated model returns, `scripts/review_conflict_resolve.sh` fails on any path-specific staging error and refuses both no-change success and `[ai-merge-resolve]` commit creation while `git diff --name-only --diff-filter=U --` reports entries. `.github/workflows/review_autofix.yml` marks resolver actuation before invocation and summarizes an attempted run without `CONFLICT_RESOLVED=true` as `conflict_resolver_failed`.
- `scripts/verify_integration_fingerprints.py` uses `FINGERPRINT_QUARANTINE_RUNS_M` to move stable unchanged drift into ai-memory quarantine and emits `FINGERPRINT_QUARANTINED_V1` markers when the skip path activates. `.github/workflows/drift-audit.yml` (cron `0 3 * * *`, gated by `DRIFT_AUDIT_ENABLED`) scans `PRE_EXISTING_FINGERPRINT_DRIFT_V1` / `FINGERPRINT_QUARANTINED_V1` markers and maintains tracker issues for persistent clusters. The audit skips any cluster whose fingerprint path is absent from the repository checkout, so markers echoed from test fixtures or PR diffs (synthetic paths such as `scripts/example.py`) do not open tracker issues. Completed runs concluded `cancelled` / `skipped` routinely upload no logs (concurrency-superseded review runs); a failed log fetch for them is classified `unscannable` rather than missing, keeping coverage `full` so the per-run Telegram summary stays at DEBUG instead of firing a daily partial-coverage WARNING; their logs are still scanned when present. Every enabled run posts a Telegram run summary (`tg_send_msg`, gated by `TG_BOT_SECRET` / `TG_ADMIN_CHAT_ID`) linking to the run and writes a GitHub Actions job summary.
- `.github/workflows/security-audit.yml` (weekly `0 8 * * 0` plus `workflow_dispatch` plus `workflow_call`, gated by `SECURITY_AUDIT_ENABLED`, default `true`) is a default-branch maintenance audit that runs on the source repo and, via the synced `workflow-templates/ai-security-audit.yml` wrapper, on every consumer repo against its own default branch (consumer runs stage this repo's `scripts/` + `prompts/` from a `@stable` support checkout into `SECURITY_AUDIT_SUPPORT_DIR` and need `OPENROUTER_API_KEY`, optionally `GH_PAT`). It runs `scripts/security_audit.sh` with `prompts/mode-security-audit.txt`, appends dated findings sections to the stable `AI Security Audit Tracker` issue (`ai:security-audit`, marker `<!-- ai:security-audit-tracker:v1 -->`), and opens one `ai:security` follow-up issue for every finding that survives confidence-gate + false-positive-exclusion filtering, with no per-run or weekly cap (the former 3-per-week cap deferred findings silently: tracker #3576, run 35996690244, surfaced 5 and filed 3). Findings whose `<!-- ai:security-finding:<id> -->` marker is already on any `ai:security` issue, open or closed, are skipped; that dedupe reads every such issue with one paginated REST listing (`gh api --paginate --slurp repos/<repo>/issues?labels=ai:security&state=all`), so it no longer stops at 200 issues, and it ignores pull requests. The orchestrator's `[security-pass] Advisory: …` issues carry the same marker and label, so the audit never re-files a finding the security pass already filed. Each completed run records the audited HEAD on the tracker body (marker `<!-- ai:security-audit-last-sha:… -->`); the next run skips entirely when HEAD is unchanged (`SECURITY_AUDIT_SKIP_IF_UNCHANGED=true`, log-only skip) and otherwise diff-scopes the audit to the commits since that SHA (`SECURITY_AUDIT_INCREMENTAL=true`; the post-filter drops findings citing unchanged files as `suppressed_out_of_scope`; first runs, history rewrites, and >200-file diffs fall back to the full scope). `.github/workflows/internal-clarify.yml` skips `ai:security-audit` issues so tracker bookkeeping never recurses into the normal clarify/plan pipeline.
- **Security dependency hold (issue #4934).** When the audit files a second finding for a file it already filed one for, the new `ai:security` issue carries one `- Depends on: #<n>` line. `scripts/security_dependency.py security-dependency --issue-json <file> --repo <owner/repo> --issue-number <n> [--number-only]` prints `{"status": "none" | "ready" | "held", "reason", "depends_on"}`; a malformed, repeated or unverifiable declaration is `held` (fail closed). Clarify (`Decide clarify route`), both implement gates and the standalone stall poller hold such an issue until #<n> is closed with `ai:merged`; the poller then posts one `/reclarify` with the `<!-- ai:security-dependency-released:<n> -->` marker. The check costs one issue read, only for an issue that declares a dependency. It moved unchanged from the retired Claude issue router.
- **Standalone clarify auto-decide (port P3).** On an issue that is not `ai:orchestrator-managed`, `clarify.yml`'s "Standalone auto-decide" step answers freshly posted questions with each question's RECOMMENDED option: `scripts/auto_decisions.py parse` builds the `Q1: A` lines and `scripts/orchestrate_parse_and_post_answer.sh` posts them with its loop guard (an exhausted guard still escalates to `ai:blocked`). Every pick becomes an `AD-<n>` entry (question, pick, why, alternatives) in the single trusted `<!-- ai:auto-decisions:v1 -->` comment, edited in place; `implement.yml` copies the entries into the PR body with `#<digits>` broken up. The existing clarify comment read is now paginated once (one API call per page), shared with semantic-cache history and auto-decide; the prompt still gets only the oldest 50, while the auto-decide fallback guard counts prior auto-answers from the full snapshot. If pagination fails, clarification stops before the answer; if the full history cannot be read at auto-decide time, it skips the answer. Skipped after a human `/reclarify`, when a question has no RECOMMENDED option, or with `STANDALONE_AUTO_DECIDE_ENABLED=false`. Costs one comment write for the answer and one for the AD comment; no additional comment read at the auto-decide step.
- **Standalone issues answered by the clarify-respond worker (issue #6262 follow-up).** With `STANDALONE_CLARIFY_RESPOND_ENABLED` (default `true`), `clarify.yml`'s auto-decide step only delegates (`delegated=true`) and its "Clarification required" Telegram alert is skipped (`AI_PHASE_GATE_V1 phase=clarify gate=tg_alert reason=delegated_to_clarify_respond`; `reason=auto_answered` after a RECOMMENDED-only answer). `orchestrate_clarify_respond.yml`, which the questions comment already triggers, decides its mode in "Check orchestrator metadata" (outputs `mode=orchestrator|standalone|skip`, `respond`, and the unchanged `is_orchestrator`): standalone needs clarify's questions comment (first line `<!-- ai:clarification-questions -->`, so plan-stage questions are not answered here), an open issue without `ai:orchestrator-managed`, no `<!-- ai:clarification-human-answer -->` (clarify appends it after a human `/reclarify`) and neither `STANDALONE_AUTO_DECIDE_ENABLED` nor `STANDALONE_CLARIFY_RESPOND_ENABLED` set to `false`. Every later step runs on `respond == 'true'`. Standalone mode reads the full comment thread once (needed by the poster's backup loop guard and the AD comment; a failed read fails the run before the model), and both modes get a host-side GITHUB FACTS block from `scripts/clarify_github_facts.py` (one aliased GraphQL call for referenced issues, PRs and branches, plus at most 5 run reads; fail-open), because the model runs network-isolated without a GitHub credential. The model step is `continue-on-error` in standalone mode only: when it fails, "Standalone RECOMMENDED fallback" posts the RECOMMENDED options, or pages `CRITICAL` when a question has none. After the poster answers, "Record standalone auto-decisions" runs `scripts/auto_decisions.py from-answers` and `render`, so each decision becomes an `AD-<n>` entry naming its decider (`clarify-respond on claude|codex`, `clarify-respond, semantic cache`, `RECOMMENDED fallback`), and each `SETUP REQUIRED:` bullet from the worker becomes a deduplicated `SETUP-<n>` item under "Setup required", which `pr-section` repeats in the PR body. Prompt contract (`prompts/mode-clarify-respond.txt`): credentials and setup never ESCALATE; the worker decides with an UPPER_SNAKE_CASE placeholder secret or variable read with no default (skip, or fail closed when skipping weakens a security control) and lists it under `SETUP REQUIRED:`; an issue with no stated intent (the release gate's `[E2E Clarify Negative Test]` body "Make it better.") has every what-to-do question escalated. `prompts/mode-clarify.txt` no longer emits `BLOCKED:` for credentials, undecided branch names or future commits; `BLOCKED:` remains for auth-walled content the task depends on. `orchestrate_clarify_respond.yml` now also stages `clarify_data_provision_guard.py`, which consumer repositories never had, so the data-provision guard was silently skipped there. Tests: `tests/test_clarify_respond_standalone.py`, `tests/test_clarify_github_facts.py`, `tests/test_auto_decisions.py`.
- **Activation verification (port P4) and operator steps (Q33).** After a merge into the default branch (`issue_pr_status.yml` job `activation-verify`) and at project completion (poller `run_project_activation_verify`, after every `emit_orchestrator_completion_lessons`), `scripts/activation_verify.sh` grades the work LIVE or DORMANT. The poller also retries completed projects with trusted partial verdict comments while fewer than three exist and until 30 minutes after the first partial comment; a failed follow-up write may be retried within that window, but failures without an initial partial comment are not retried after completion. PR mode gets the merged file list from the paginated PR-files API, falling back to the merge diff only for a merge commit or a known single-commit PR; project mode reads the final PR's file list or the state's planned file hints when no final PR exists. Missing/incomplete scope skips verification rather than grading only part of a rebase. Project verification waits for a complete tracking-comment fetch before deduplicating, trims trailing whitespace from verdict comments, and uses a unique worktree and runtime directory that are cleaned up after the run. The model's OpenRouter key is redacted from normalized text before posting it to GitHub. Markers: `<!-- ai:activation:v1 verdict=<V> source=<pr-N|project-N> -->` on a complete verdict comment (the poller trusts only a terminal marker by a repository-associated author), `<!-- ai:activation:v1 partial=true source=<pr-N|project-N> -->` on an incomplete verdict, `<!-- ai:activation-fix:v1 source=... -->` as the first line of the code-gap issue (a merge that closes such an issue is not verified again), and the `ai:operator-step` issue (`<!-- ai:operator-step:v1 -->`, one `<!-- ai:operator-step:entry key=<key> -->` section per source, replaced in place) written only by `scripts/operator_step_issue.py`. A failed fix-issue lookup does not create another issue or finalize the verdict; operator steps and a non-terminal comment still surface the failure (PR mode needs a rerun after recovery). The operator-step writer reconciles observed duplicates and reads its just-created issue directly if the label list lags, with bounded retry backoff. Whole-body GitHub PATCHes are not atomic across independent writers: concurrent upserts can still lose an entry. `ai:operator-step` is excluded from issue-opened clarification. Kill switch `ACTIVATION_VERIFY_ENABLED` (default `true`).
- `scripts/security_audit.sh` exposes `SECURITY_AUDIT_OUTPUT_MODE=findings-json` for the default-on orchestrator project security pass. It accepts an optional project-spec file, supports a fail-closed explicit `SECURITY_AUDIT_DIFF_BASE`/`SECURITY_AUDIT_DIFF_HEAD` range, optionally narrows that range with `SECURITY_AUDIT_DIFF_SINCE` (only range files changed since that commit stay in scope; fails closed on an unresolvable or non-ancestor commit) and re-verifies `SECURITY_AUDIT_PRIOR_FINDINGS` (a JSON array of earlier findings whose files stay in scope and which the prompt asks the model to re-emit under the same ID if they persist, alongside every remaining instance of the same class; fails closed on malformed input), applies the existing validation/exclusion/confidence/scope filters, and atomically publishes `security_audit_findings.v1` to `SECURITY_AUDIT_FINDINGS_OUT`. This mode performs no GitHub tracker, label, follow-up, last-SHA, or notification side effects; the default `issues` path remains the production weekly mode.
- On a Codex execution failure, `scripts/security_audit.sh` emits only a sanitized stderr tail (at most 40 lines and 4,096 rendered bytes) between `security-audit: codex-stderr-tail begin/end` markers and adds `provider=402|401|429|5xx|unknown` to the existing failure line; successful runs emit no tail.
- `.github/workflows/workflow-log-analysis.yml` now also has a source-repo-only weekly retro path (cron `0 9 * * 1`, gated by `WORKFLOW_RETRO_ENABLED`, default `true`). `WORKFLOW_RETRO_CRON` defaults to the same cron string and must stay in sync with the trigger because GitHub does not interpolate vars into `on.schedule`. The workflow builds retro context with `scripts/workflow_retro.py`, renders the narrative through `prompts/mode-workflow-analysis.txt` in retro mode using `WORKFLOW_RETRO_MODEL` / `WORKFLOW_RETRO_REASONING` (defaults `openai/gpt-6-luna` / `medium`), and posts into the stable `AI Workflow Weekly Retro` tracker issue (`ai:retro`, marker `<!-- ai:retro-tracker:v1 -->`). Zero-activity windows (no workflow runs and no merged PRs; `has_activity: false` in the `workflow_retro.v1` JSON) skip the LLM pass and the tracker comment when `WORKFLOW_RETRO_SKIP_IF_NO_ACTIVITY=true` (default), leaving only a `WORKFLOW_RETRO_SKIP_V1:` line in the run log and no Telegram alert. After the source-repo retro, the `Consumer retro fan-out` step (gated by `WORKFLOW_RETRO_CONSUMER_FANOUT_ENABLED`, default `true`) runs `scripts/workflow_retro_fanout.sh`: for each repo in `.github/ai/consumer_repos.json` (source repo excluded) it builds a per-repo retro from the same collect-logs artifact, honors the consumer's own `WORKFLOW_RETRO_ENABLED` repo var (one fail-open `gh api` GET per consumer per week), applies the same no-activity skip, and upserts the week-marked comment on that consumer's `AI Workflow Weekly Retro` tracker via `GH_PAT` (§14 repo scope). Per-repo outcomes are logged as `WORKFLOW_RETRO_FANOUT_V1: repo=… status=posted|refreshed|up_to_date|skipped_no_activity|skipped_disabled|failed`; individual failures fail open and the step errors only when every attempted consumer fails. Both `.github/workflows/internal-clarify.yml` (source repo) and the consumer-facing gate in `.github/workflows/clarify.yml` skip `ai:retro` / `ai:security-audit` issues so tracker upkeep never recurses into the clarify/plan pipeline.
- `scripts/orchestrate_poll_process.sh` gates last-resort `orchestrator/project-*` branch rebuilds behind `BRANCH_REBUILD_ENABLED`, `BRANCH_REBUILD_THRESHOLD_HOURS`, and `BRANCH_REBUILD_COOLDOWN_HOURS`. Audit snapshots are persisted as `BranchRebuildAuditV1` in `ai-memory/schemas/branch_rebuild_audit.v1.json` (this shipped artifact supersedes the old plan placeholder name `BRANCH_REBUILD_AUDIT_V1`; there is no literal runtime marker with that string).

## Operational lessons learned (categorised)

**General / Tooling**
- Treat the `openai/codex#11151` no-edit regression as closed only with function-style patch tooling; keep `apply_patch_tool_type = "function"` as the settled baseline. Pointers: `scripts/codex_model_catalog.json`, `scripts/write_codex_config.sh`.
- Keep `low` as the default gpt-6-sol verbosity across workflow entrypoints unless a specific phase re-proves the old announce-without-emit failure. Pointers: `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`, `.github/workflows/orchestrate.yml`.

**codex-cli quirks**
- Announce-without-emit is a known codex-cli failure mode on patch-heavy turns; the mitigation is to keep patch tooling explicitly enabled rather than raising verbosity by default. Pointers: `scripts/codex_model_catalog.json`, `.github/workflows/implement.yml`.
- The OpenRouter Responses-path regression was tied to `apply_patch_tool_type: "freeform"`; keep `include_apply_patch_tool = true` and function-style patch wiring in editor phases. Pointers: `scripts/codex_model_catalog.json`, `.github/workflows/clarify.yml`, `.github/workflows/plan.yml`, `.github/workflows/implement.yml`.

**OpenRouter / prompt-cache**
- Prompt-cache hit rate depends on stable prompt ordering and unchanged prefix blocks; preserve cache-friendly layout before adding new dynamic material. Pointers: `probably_unnecessary_but_read_if_stuck.md` (OpenRouter Prompt Cache Instrumentation / Semantic Cache Scope), `scripts/openrouter_prompt_cache.py`.
- `OPENROUTER_PROMPT_CACHE_DISABLED` is the explicit kill switch, and Gemini-family models may skip cache breakpoints when the reviewer path marks them incompatible. Pointers: `probably_unnecessary_but_read_if_stuck.md`, `scripts/review_run_reviewers.sh`.

**GitHub API rate-limits**
- Shared GitHub quota handling is reset-aware: use the repo helpers' `gh_retry` backoff behavior instead of ad-hoc retry loops. Pointer: `scripts/gh_helpers.sh`.
- A retry wrapper must buffer each attempt's stdout and emit only the successful attempt's: `gh api` prints failed responses to stdout, so an unbuffered retry prepends error bodies to the real output (#5495). `gh_retry` does this; an inline `"$@"` retry loop does not. Pointers: `scripts/gh_helpers.sh`, `tests/test_gh_retry_stdout_isolation.py`.
- Rate-limit alerting is deduplicated by pin/cooldown state, and repeated issue/PR lookups should flow through the poller's batched GraphQL helpers. Pointers: `scripts/gh_helpers.sh`, `scripts/orchestrate_poll_process.sh`.

**Memory subsystem**
- The `ai-memory` branch is the canonical backing store; consumers must fail open when memory reads or writes are unavailable. Pointers: `scripts/memory_helpers.sh`, `scripts/ai_memory.py`.
- `AI_MEMORY_TELEMETRY` and the per-PR review ledger are continuity surfaces, not hard gates; preserve ledger identity across reruns. Pointers: `scripts/ai_memory.py`, `scripts/review_issue_ledger.sh`.
- Lessons-learned memory uses the standalone schema `ai-memory/schemas/lessons_learned_record.v1.json`; review-autofix writes issue-scoped records under `ai-memory/tasks/issue-*/lessons_learned/` via `scripts/ai_memory_lib.py::record_lessons_learned`, and plan-mode prompts treat surfaced same-file lessons as soft priors rather than hard requirements.
- Operator-facing memory hygiene lives in `scripts/ai_memory.py`: `review --since <duration>` lists stale task candidates, `prune --record-id <id>` marks task candidates for the existing monthly `compact --prune true` archival path, `search --query <text>` prefers OpenRouter embeddings when `OPENROUTER_API_KEY` is set and otherwise falls back to keyword ranking, and `export --issue <n>` / `--pr <n>` dumps matching memory records as JSON. `prune` is intentionally additive: it writes a `timestamps.prune_marked_at` marker on candidate records instead of introducing a second maintenance channel.
- Prompt retrieval reads lessons-learned records too: `retrieve_memory_context` (`scripts/ai_memory_lib.py`) appends a `LESSONS LEARNED (soft priors from earlier runs; not requirements)` block for the `planning`, `implementation`, and `reviewer` roles with up to 5 of the newest `tasks/*/lessons_learned/*.json` records whose text or tags match the issue keywords. They use at most a quarter of the role's token budget (`LESSONS_RETRIEVAL_BUDGET_FRACTION`); records keep the rest, and with no matching lesson the context is unchanged. Invalid lesson files are skipped; `LESSONS_LEARNED_ENABLED=false` turns the block off. The retrieve JSON and telemetry gain `lessons_selected` (and `lesson_ids` in the JSON).
- The orchestrator writes a retrospective when a project completes, including clean-skip completions where the judge never runs. `scripts/orchestrate_poll_process.sh` records causes as they happen in state `lesson_events` (newest 20, via `orchestrate_lib.py append-lesson-event`): judge fix-up issue created (`create_judge_fixup_issues_from_verdict`), validation `needs_fixes` diagnosis (`sync_validation_fix_issues_from_comments`), security findings reported (`run_security_pass_inline`, after `SECURITY_PASS_BLOCKED`), and orchestrator stall recovery. On each of the four `status = "complete"` paths, `emit_orchestrator_completion_lessons` turns the events plus the recovery / judge-stall / review-blocked / validation / security counters into `lessons_learned_record.v1` records (`orchestrate_lib.build_completion_lessons`; phase `orchestrator_completion`, kind `project_retrospective`, tags `source:*`, `project:<tracking>`, `file:*`) under `ai-memory/tasks/issue-<tracking>/lessons_learned/`. Record ids are deterministic and existing ids are skipped, so a repeated completion tick writes nothing; a clean project with no events or counters writes nothing. Fail-open, no GitHub API calls; gated by `AI_MEMORY_ENABLED` / `LESSONS_LEARNED_ENABLED`.

**Validation harness Docker lifecycle**
- Validation containers distinguish `/bin/sh -c` from `/bin/sh -lc`; shell choice is part of harness correctness, not a cosmetic variation. Pointers: `scripts/validation_lint.py`, `prompts/mode-validate-generate.txt`.
- npm/yarn/pnpm wrapper shutdown handling and `mongosh` apt-repo constraints are harness invariants; keep the existing SIGTERM/exit-code and package-source rules intact. Pointers: `scripts/validate_driver.sh`, `prompts/mode-validate-fix-harness.txt`.

## Repo-tree (auto-generated)

Active workflow files (regenerate with `make generate`):

<!-- TREE:START id=workflows -->
```
.github/workflows/audit_consumer_drift.yml
.github/workflows/auto-release-stable.yml
.github/workflows/cancel_on_pr_close.yml
.github/workflows/check_failure_triage.yml
.github/workflows/ci.yml
.github/workflows/clarify.yml
.github/workflows/claude-engine-smoke.yml
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
.github/workflows/internal-workflow-failure-heal.yml
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
.github/workflows/sync-claude-live-copies.yml
.github/workflows/sync_ai_labels.yml
.github/workflows/test-and-mark-stable.yml
.github/workflows/update_workflows.yml
.github/workflows/validate.yml
.github/workflows/validation-improvements-intake.yml
.github/workflows/validation-refresh.yml
.github/workflows/workflow-failure-heal-intake.yml
.github/workflows/workflow-log-analysis.yml
.github/workflows/workflow_failure_heal.yml
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
workflow-templates/ai-workflow-failure-heal.yml
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

## Conflict-safe PR-close cleanup

- `.github/workflows/internal-cancel-on-pr-close.yml` and `workflow-templates/ai-cancel-on-pr-close.yml` retain the immediate `pull_request.closed` path and also run every five minutes. The schedule covers conflicted PRs for which GitHub suppresses the close event without introducing `pull_request_target` trust.
- Scheduled mode in `.github/workflows/cancel_on_pr_close.yml` snapshots queued and in-progress `pull_request` runs, resolves every linked PR through aliased GraphQL batches of at most 50, and cancels a run only when every association is known and terminal (`CLOSED` or `MERGED`). Missing, malformed, open, or partial state preserves the run. Event mode remains branch- and PR-scoped.
- Cleanup jobs share repository-scoped concurrency. Scheduled ticks run the merge train's existing global release scan; event runs retain the closed PR's base filter.
- The release gate's Phase 7 (`test-and-mark-stable.yml`, step `Phase 7: Close PR and verify cancel_on_pr_close fires`) makes the smoke PR mergeable before closing it. GitHub does not fire `pull_request.closed` for a conflicted PR, and the scheduled sweep's observed cadence (median about 12 minutes) is longer than `PHASE7_WAIT_BUDGET_MINUTES`, so run 35672590166 failed `no_run` after the forward-merge of `stable` rewrote the same `.ai/.workspace_source_manifest.txt` hunk as the smoke PR 75 seconds before the close. The step merges the base into `ai/issue-N` through the merges API; on a 409 it overwrites each PR file the base also changed since the merge base with the base's version (contents API, `[E2E Smoke Test]` commit prefix), retries once, and waits for mergeability to be recomputed. It never fails the gate on its own: the outcome is logged as `PHASE7_UNCONFLICT_CHECK` / `PHASE7_UNCONFLICT_FILE` / `PHASE7_UNCONFLICT_RESULT`, exported as the step output `unconflict`, and shown in the results table. `tests/test_test_and_mark_stable_phase7_unconflict.py` pins the contract.
