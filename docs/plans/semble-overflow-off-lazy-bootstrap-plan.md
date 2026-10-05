# Semble Overflow-Path Shutdown, Lazy Bootstrap and Honest Telemetry

## Summary

Switch off Semble's "overflow" lookup, which pastes the same unrelated chunks
under the names of files too large to inline. Stop installing and indexing
Semble in workflows that never query it, and install it lazily on first use
in the workflows that do. Add telemetry that measures what Semble actually
contributes. Serena is out of scope and stays off.

## Automation & Wiring

- **Scripts:** Extend existing Semble bootstrap, query and telemetry helpers;
  introduce no standalone script.
- **Entry points and triggers:** The reusable `.github/workflows/implement.yml`
  runs through `.github/workflows/internal-implement.yml` on an
  `issue_comment: created` `/approved` command; the reusable
  `.github/workflows/review_autofix.yml` runs through
  `.github/workflows/internal-review.yml` on its existing `pull_request`,
  `push` and `workflow_dispatch` triggers;
  `.github/workflows/orchestrate_poll.yml` runs through
  `.github/workflows/internal-orchestrate-poll.yml` on its `*/5 * * * *`
  schedule and manual dispatch; `.github/workflows/validate.yml` runs through
  `.github/workflows/internal-validate.yml` on `workflow_dispatch`. The
  existing `.github/workflows/workflow-log-analysis.yml` collects telemetry on
  its `0 9 * * 1` schedule and manual dispatch. No new trigger is needed;
  Phase 2 removes unused bootstrap from the existing clarify, plan,
  orchestrate and clarify-respond entry points.
- **Supervisor:** None; all work occurs in the existing finite workflow jobs.
- **Database gate:** Not applicable; no database operations or contracts change.
- **Future-removal registry:** No new single-use or long-running script or
  supervisor, so no `docs/scripts-pending-removal.md` entry is required.

## Context

An audit of the unattended pipeline's context tools (interactive session,
2026-10-05) covered three sources:

- 112 sampled runs from 2026-07-10 to 2026-09-24. Older logs have expired.
- 2,510 Serena efficiency reports posted on PRs from 2026-03-22 to 2026-05-03.
- Roughly 60 historical analysis docs recovered from git history.

Findings that motivate this plan:

1. **The overflow lookup injects misleading context.**
   - When a file does not fit `TARGETED_FILE_CONTEXT_MAX_BYTES`,
     `scripts/targeted_file_context.py` queries the whole-repo BM25 index with
     `f"{rel}\n{semble_query_text}"` (line ~595). It renders the result as
     `--- FILE: <rel> (N bytes — chunk-retrieved via semble) ---`.
   - In the implement path the query text is the entire plan (line ~770,
     `_read_optional_query_text(args.semble_query_from) or plan_text`). The
     plan text swamps the file path in the search.
   - Replaying issue #6235's plan against a locally built index gave every
     overflowing file the **same** chunks: six each for `scripts/security_audit.sh`,
     `scripts/workflow_failure_heal_intake.sh`, `scripts/orchestrate_poll_process.sh`
     and `.github/workflows/orchestrate_poll.yml`, plus the first two of them
     for `.github/workflows/workflow-failure-heal-intake.yml`. **None** of the
     chunks came from the named file; they came from a plan doc, `README.md`
     and `scripts/claude_engine.py`.
   - The CI log for implement run 37245942197 shows the same pattern: four
     different files, each with a 7,560-byte block.
   - The model is told a block "is" the file, but the block contains another
     file's text. Part of it duplicates `README.md`, which is already in the
     static prefix.
2. **The duplication is common, and it has caused an outage.**
   - The sample held 32 genuine overflow queries (after removing echoed
     telemetry lines). 18 of them (56%) had exactly the byte size of an
     earlier block in the same run.
   - Implement run 33796624872 (2026-09-03, issue #3990) rendered ten
     128,128-byte blocks, 9 of them the same size. The prompt passed codex's
     1,048,576-character stdin cap and every attempt failed. The later clamp
     (`TARGETED_FILE_CONTEXT_MAX_BYTES` as a true total) bounds the size but
     does not fix the content.
3. **Bootstrap is paid where Semble is never queried.**
   - Install takes about 10 s and install plus index about 32–34 s per
     implement or plan job.
   - `clarify.yml`, `plan.yml`, `orchestrate.yml` and
     `orchestrate_clarify_respond.yml` install and index Semble but have no
     query path at all. `tests/test_semble_workflow_parity_contract.py:86`
     explicitly forbids adding one "yet".
   - Pollers installed Semble in 20 of 22 sampled runs with 0 queries.
   - Implement queried it in 4 of 25 runs, all of them overflow queries.
     Repair and diagnose query it only on failure.
4. **No measured saving exists.** Every workflow-optimization report from
   2026-05-14 to 2026-10-03 calls Semble's value "unproven" or "inferred".
   - The telemetry is also exposed to echoed lines. `SEMBLE_QUERY` text
     pasted into a prompt, an issue body or test output is re-counted by
     `scripts/cost_audit.py`.
   - The audit found identical overflow lines, with identical `ms=` values,
     in two different runs' logs.

The task-text lookups (`semble_query_block` callers: reviewer context,
conflict-resolver context, judge prefetch, validate discover and diagnose,
implement repair and diagnose, workflow-log-analysis prefetch) are kept
unchanged. Their queries are built from task-specific text, not a whole plan.

The decisions taken in the clarification round are recorded under
[Decisions](#decisions). Every question was answered `A`.

## Decisions

### D1 — Which callers lose the overflow lookup

- **Chosen:** All four: implement (`implement.yml`), review editor
  (`review_apply_fixes.sh`), reviewer scope (`review_run_reviewers.sh`) and
  conflict resolver (`review_conflict_resolve.sh`). (Q1: A)
- **Alternatives considered:** Implement only. That caller uses the full plan
  text as the query and caused #3990.
- **Why:** All four issue the same unrestricted whole-repo query, so they
  share the same off-file and duplicate-chunk failure mode.

### D2 — How the overflow lookup is switched off

- **Chosen:** A new switch, `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED`,
  default `false`. Callers pass `--semble-*` only when it is `true`.
  `targeted_file_context.py` keeps its CLI flags. (Q2: A)
- **Alternatives considered:** Drop the flags at the call sites with no
  switch. Delete the overflow code and its flags from the Python script.
- **Why:** The switch keeps the change reversible via a repo variable and
  removes no identifiers (§6).

### D3 — Workflows that install Semble but never query it

- **Chosen:** Remove the setup-uv, Install semble and Build semble index steps
  from `clarify`, `plan`, `orchestrate` and `orchestrate_clarify_respond`.
  Rewrite `tests/test_semble_workflow_parity_contract.py` to assert their
  absence. (Q3: A)
- **Alternatives considered:** Gate the steps behind a new switch that
  defaults to off. Move them to lazy bootstrap (it would never fire).
- **Why:** They have no query path; the parity test forbids one. The steps
  cost about 30 s per run for nothing.

### D4 — Bootstrap timing in the workflows that do query Semble

- **Chosen:** A `semble_ensure_ready` helper installs and indexes on the first
  `semble_query_block` call, at most once per job (`flock`-guarded, with a
  state file).
  - `implement`, `review_autofix`, `orchestrate_poll` and `validate` use it,
    controlled by `SEMBLE_BOOTSTRAP_MODE` (`lazy` | `eager`, workflow
    default `lazy`; helper default `eager` when unset).
  - `workflow-log-analysis` stays eager.
  
  (Q4: A)
- **Alternatives considered:** Always lazy, with no switch. Make only the
  poller lazy.
- **Why:** Most implement, plan and poller jobs never query Semble. The
  switch gives a repo-variable rollback. `workflow-log-analysis` queries on
  every run, so lazy install would gain nothing there.

### D5 — What a lazy index reflects

- **Chosen:** The workspace at the time of the first query. (Q5: A)
- **Alternatives considered:** Snapshot HEAD with `git archive` so results
  match eager behaviour exactly.
- **Why:** It needs less code and time. On repair and diagnose, including the
  model's in-progress edits is arguably more accurate. The difference is
  documented.

### D6 — Telemetry scope

- **Chosen:** Measured fields only:
  - bytes injected;
  - distinct chunk sources (`sources=`);
  - bytes duplicating the static prefix (`static_dup_bytes=`);
  - bootstrap timing (`SEMBLE_BOOTSTRAP`);
  - an unused-bootstrap aggregate.
  
  There is no holdout and no "bytes avoided" estimate. (Q6: A)
- **Alternatives considered:** Add a holdout cohort,
  `SEMBLE_PREFETCH_HOLDOUT_PERCENT`, defaulting to 10% or to 0%.
- **Why:** The user chose to measure only what can be observed directly,
  without degrading any production run's context.

### D7 — Echoed telemetry lines

- **Chosen:** Add `run=<GITHUB_RUN_ID>-<GITHUB_RUN_ATTEMPT>` to `SEMBLE_*`
  lines. `cost_audit.py` and `collect_workflow_logs.py` drop lines whose
  `run=` does not match the log being parsed. Lines without `run=` stay
  accepted. (Q7: A)
- **Alternatives considered:** Keep the current parser.
- **Why:** The audit found identical overflow lines, with identical `ms=`
  values, in two different runs' logs. Echoed telemetry inflates every
  report.

### D8 — Phase split

- **Chosen:** Four independently mergeable phases: overflow off and README;
  unused bootstrap removed; lazy bootstrap; telemetry. (Q8: A)
- **Alternatives considered:** Three phases, or two phases.
- **Why:** Each phase has its own rollback and can merge in any order.

## Goals

- G1: Semble must not run for the overflow path unless
  `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED=true`.
  - Verifiable: zero `SEMBLE_QUERY target=overflow` events in implement and
    review runs after Phase 1.
  - Oversized files get the existing `would overflow total budget — read with
    read tool` marker.
- G2: `clarify`, `plan`, `orchestrate` and `orchestrate_clarify_respond` no
  longer install or index Semble.
  - Verifiable: no `install_semble:` or `build_semble_wrapper:` lines in their
    logs.
- G3: With `SEMBLE_BOOTSTRAP_MODE=lazy`, `implement`, `review_autofix`,
  `orchestrate_poll` and `validate` install and index Semble only on the
  first `semble_query_block` call of a job, at most once per job.
  - Verifiable: a poller tick with no judge query has no `install_semble:`
    lines.
  - A job with N queries has exactly one `SEMBLE_BOOTSTRAP` event.
- G4: Every task-text Semble lookup keeps working in lazy mode, and still
  fails open when Semble is unavailable.
- G5: `SEMBLE_QUERY` and `SEMBLE_FALLBACK` carry `run=`, `sources=` and (when a
  static prefix is known) `static_dup_bytes=`. A new `SEMBLE_BOOTSTRAP` event
  carries mode, state and timings.
  - `cost_audit.py` reports bootstraps, unused bootstraps, bootstrap time,
    static-duplicate bytes and dropped echo lines.
- G6: `README.md:1645` and `probably_unnecessary_but_read_if_stuck.md:749` state
  the real `SEMBLE_ENABLED` default (`true`).

## Non-goals

- Serena. It stays dormant with `SERENA_ENABLED` default `'false'`, and none
  of its steps, scripts or tests change (user decision Q1: A in the audit
  round). The setup-uv step stays wherever Serena needs it.
- Deleting the overflow code path or the `--semble-*` flags from
  `scripts/targeted_file_context.py` (§6). They stay functional for explicit
  callers.
- Improving retrieval quality, such as restricting a query to one file or
  switching from BM25 to embeddings.
- A holdout or A/B cohort, or any "bytes avoided" estimate (Q6: A).
- Changing `workflow-log-analysis.yml` bootstrap behaviour (Q4: A).
- Removing the `SEMBLE_ENABLED` env line, runtime default exports or
  support-file staging entries from the four Phase 2 workflows (minimal
  change, §5).
- Any change to consumer-repo wrapper templates in `workflow-templates/`.

## Constraints

- **§6 naming immutability.**
  - No existing identifier is renamed.
  - Removed: the setup-uv, Install semble and Build semble index **steps** in
    four workflows, plus the obsolete test functions in
    `tests/test_semble_workflow_parity_contract.py`. Both removals were
    explicitly approved (Q3: A).
  - New identifiers were checked for collisions against the whole repo (0
    matches each): `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED`,
    `SEMBLE_BOOTSTRAP_MODE`, `SEMBLE_BOOTSTRAP_STATE_FILE`,
    `SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS`, `SEMBLE_BOOTSTRAP` (event),
    `SEMBLE_STATIC_CONTEXT_FILE`, `semble_ensure_ready`, `semble_should_query`.
  - New log fields: `static_dup_bytes`, `sources`, `run`.
    `SEMBLE_INSTALL_MS` has no existing use in scripts, workflows or tests;
    re-check any further name before adding it.
- **§4 env vars.** Every new variable has a default:
  - `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED=false`
  - `SEMBLE_BOOTSTRAP_MODE=lazy` in the four opted-in workflows, and
    **`eager` when unset** in the helper
  - `SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS=180`
  - `SEMBLE_BOOTSTRAP_STATE_FILE` defaults to
    `${RUNTIME_DIR:-${RUNNER_TEMP:-/tmp}}/semble_bootstrap-${GITHUB_RUN_ID:-${BASHPID}}-${GITHUB_RUN_ATTEMPT:-1}.state`;
    resolve and export it once when sourcing the helper so subshells share
    one path, including when neither GitHub ID nor `RUNTIME_DIR` is set.
  - `SEMBLE_STATIC_CONTEXT_FILE` is unset by default, in which case the field
    is omitted.
- **§9 style.** Tabs in shell and Python, 2-space YAML, opening braces on a
  new line for shell functions as in `scripts/semble_helpers.sh`.
- **§14 consumers.** Every change is in reusable workflows and staged support
  scripts, so consumer repos pick it up on the next `@stable` tag. Wrappers in
  `workflow-templates/` are unchanged.
- **§15 GitHub API.** No new `gh api` or MCP calls. `cost_audit.py` already
  fetches `databaseId` in its run listing; Phase 4 requests `attempt` from
  that same listing if the pinned CLI supports it. `collect_workflow_logs.py` already
  fetches `id` and `run_attempt` through its existing REST listing.
- **§18 automation.** No new standalone scripts. All logic extends
  `scripts/semble_helpers.sh`, `scripts/install_semble.sh`,
  `scripts/build_semble_wrapper.sh`, `scripts/targeted_file_context.py`,
  `scripts/cost_audit.py` and `scripts/collect_workflow_logs.py`. Nothing
  needs an entry in `docs/scripts-pending-removal.md`.
- **§20 changelog.** Each implementation phase adds one
  `changelog.d/<phase-issue-or-pr>-<slug>.md` fragment, using that phase's
  assigned issue or PR number when its implementation PR is created. This
  plan does not implement the Semble changes; the PR also includes a
  merged-PR guard parser fix that changes observable behavior and requires a
  changelog fragment. Never edit `CHANGELOG.md` directly.
- **§27 workflow size.**
  - `.github/workflows/review_autofix.yml` is 445,389 bytes, about 34.6 KB
    under the 480,000-byte split threshold. Phases 1 and 3 may add only a
    handful of lines there; check with `wc -c`.
  - `implement.yml` is 316,980 bytes.
- **§10 MongoDB.** Not touched.

## Approach

**Overflow off (Phase 1).** Three script callers already build an argument
array and append the `--semble-*` flags only under a guard; the implement
workflow passes them directly and must first use an argument array. Guard all
four on `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED == true`, so no Python
change is needed and the marker fallback becomes the default.

**Dead bootstrap removed (Phase 2).** Delete the three Semble steps from four
workflows that never query Semble, and rewrite the parity test to pin the
absence.

**Lazy bootstrap (Phase 3).**
- `semble_ensure_ready` in `scripts/semble_helpers.sh` runs the existing
  `install_semble.sh` and `build_semble_wrapper.sh` on demand. It
  serialises on `flock` and records the outcome in a state file, so callers
  inside `$(...)` subshells see it.
- `semble_query_block` calls `semble_ensure_ready` before its availability
  checks. Outer caller gates that test `SEMBLE_INDEX_AVAILABLE=true` before
  calling `semble_query_block` switch to `semble_should_query`.
- The four workflows keep their eager steps but gate them on a mode other
  than `lazy`, matching the helper's eager fallback for invalid values;
  Serena retains its independent `setup-uv` gate.
- The helper's built-in default is `eager` when the variable is unset, so
  workflows and scripts that never set it behave exactly as today.

**Telemetry (Phase 4).** Add fields to the two existing emitters and a
bootstrap event emitted by `build_semble_wrapper.sh`. The bootstrap event is
emitted on the same code path in eager and lazy mode, which keeps Phase 4
independent of Phase 3. Parsers gain run-ID echo rejection that stays
backward compatible.

**Alternatives rejected.**
- Restricting the overflow query to the named file. That is a retrieval
  redesign, out of scope.
- Deleting the overflow code (§6).
- A holdout cohort (Q6: A).
- Snapshotting HEAD for the lazy index (Q5: A).

## Phases & Merge Strategy

Each phase is one PR, independently mergeable in any order,
production-safe at merge, with its own changelog fragment and rollback.

| # | Scope | Files | Done when | Rollback |
|---|---|---|---|---|
| 1 | Overflow lookup off by default; fix the stale README / `probably_unnecessary…` defaults | `implement.yml`, `review_autofix.yml`, `orchestrate_poll.yml` (env only), `review_apply_fixes.sh`, `review_run_reviewers.sh`, `review_conflict_resolve.sh`, `README.md`, `probably_unnecessary_but_read_if_stuck.md`, tests, fragment | Contract tests prove all four call sites gate `--semble-*` on the switch, default `false`; the marker fallback is exercised | Set repo var `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED=true`, or revert the PR |
| 2 | Remove the unused bootstrap from four workflows | `clarify.yml`, `plan.yml`, `orchestrate.yml`, `orchestrate_clarify_respond.yml`, `tests/test_semble_workflow_parity_contract.py`, `README.md` (rollout note), fragment | Parity test asserts the steps are absent and the fail-open runtime defaults are still present; workflow YAML parses | Revert the PR |
| 3 | Lazy bootstrap for `implement`, `review_autofix`, `orchestrate_poll`, `validate` | `semble_helpers.sh`, `orchestrate_poll_process.sh`, `review_rb_judge.sh`, `review_conflict_resolve.sh`, `review_run_reviewers.sh`, four workflows, `README.md`, tests, fragment | `tests/test_semble_helpers.py` covers lazy ready, failed, subshell, concurrency and eager/unset modes; the contract tests pin the `eager` gating | Set repo var `SEMBLE_BOOTSTRAP_MODE=eager`, or revert the PR |
| 4 | Telemetry fields, `SEMBLE_BOOTSTRAP` event, echo rejection | `semble_helpers.sh`, `targeted_file_context.py`, `install_semble.sh`, `build_semble_wrapper.sh`, `cost_audit.py`, `collect_workflow_logs.py`, `agents.md`, workflows (static-context env lines and eager event relay), tests, fragment | Parser and workflow contract tests cover new fields, redirected events, legacy lines, mismatched `run=` and bootstrap aggregates | Revert the PR. All fields are additive and old parsers ignore unknown fields |

**Cross-phase independence notes.**
- Phases 1 and 3 touch nearby lines in `review_conflict_resolve.sh` and
  `review_run_reviewers.sh`, but different statements.
  - Phase 1 edits the `targeted_file_context` argument guards at
    `review_conflict_resolve.sh:2023` and `review_run_reviewers.sh:2080`.
  - Phase 3 edits the `semble_query_block` outer gates at
    `review_conflict_resolve.sh:2045` and `review_run_reviewers.sh:2983`.
  - Whichever merges second rebases trivially.
- Phase 3 deliberately leaves the overflow argument guards
  (`SEMBLE_INDEX_AVAILABLE=true`) unchanged. In lazy mode those guards are
  false unless an earlier query in the same job already bootstrapped Semble.
  Merging Phase 3 before Phase 1 therefore only reduces overflow usage
  (which Phase 1 intends anyway), and merging it after has no interaction.
  Re-enabling overflow later needs `SEMBLE_BOOTSTRAP_MODE=eager`; Phase 3
  documents this in README.
- Phase 4's `SEMBLE_BOOTSTRAP` event is emitted from `build_semble_wrapper.sh`,
  which both the eager steps and Phase 3's lazy helper call. It therefore
  works with or without Phase 3: `mode=` comes from
  `${SEMBLE_BOOTSTRAP_MODE:-eager}`. Phase 3 measures and exports the lazy
  install time for the builder even before Phase 4 introduces the event;
  when Phase 4 merges first, no lazy helper exists yet. The eager review and
  poller steps must relay the event from their captured index logs.
- Phase 2 only touches workflows that Phases 1, 3 and 4 do not change for
  Semble bootstrap. Phase 4's `SEMBLE_STATIC_CONTEXT_FILE` env line is added
  only to the workflows that query Semble.

## Implementation Steps

### Phase 1 — Overflow lookup off by default

1. `.github/workflows/implement.yml` (top-level `env:` block, next to line 30
   `SEMBLE_ENABLED`): add
   `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED: ${{ vars.TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED || 'false' }}`.
2. `.github/workflows/implement.yml:2319-2328` (the
   `python3 scripts/targeted_file_context.py` call):
   - Convert it to an argument array.
   - Append `--semble-bin`, `--semble-index`, `--semble-max-chunks "6"` and
     `--semble-fallback "marker"` only when both conditions hold:
     `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED` lower-cases to `true`,
     and `SEMBLE_INDEX_AVAILABLE` is `true`.
   - Keep `--plan-file`, `--repo-root`, `--max-bytes`, `--output` and the
     `|| echo "::warning::…"` fail-open line verbatim. Update the comment
     above the call to explain the default.
3. `.github/workflows/review_autofix.yml`: add the same `env:` line next to
   line 255. That is the only change to this file in Phase 1, so confirm with
   `wc -c` that it stays under 480,000 bytes.
4. `.github/workflows/orchestrate_poll.yml` (top-level `env:`): add the same
   line, so the integration-sync conflict resolver, which runs
   `review_conflict_resolve.sh`, can be re-enabled by the same repo variable.
5. Extend the three script guards, each with
   `&& [ "$(printf '%s' "${TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ]`.
   Each guard currently reads
   `if [ "${SEMBLE_INDEX_AVAILABLE:-false}" = "true" ] && [ -s "<query file>" ]`.
   - `scripts/review_apply_fixes.sh:1024`
   - `scripts/review_run_reviewers.sh:2080`
   - `scripts/review_conflict_resolve.sh:2023`

   Do **not** touch the separate `semble_query_block` "Conflict Resolver
   Context" block at `review_conflict_resolve.sh:2045` or "Reviewer Context"
   at `review_run_reviewers.sh:2983`. Those are kept task-text lookups.
6. `README.md`:
   - Line 246 (`TARGETED_FILE_CONTEXT_MAX_BYTES` row): state that the Semble
     overflow fallback runs only when
     `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED=true`, and why: the
     unrestricted query returned the same off-file chunks for every
     overflowing file (issue #6235 replay; run 33796624872 / #3990).
   - Add a new row for `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED`
     (default `false`; read by implement, review_autofix, orchestrate_poll;
     callers `implement.yml`, `review_apply_fixes.sh`,
     `review_run_reviewers.sh`, `review_conflict_resolve.sh`).
   - Line 1645 (`SEMBLE_ENABLED` row): change the default from `` `false` ``
     to `` `true` `` (reusable workflows read `vars.SEMBLE_ENABLED || 'true'`
     since #2446).
7. `probably_unnecessary_but_read_if_stuck.md:749`: correct the same stale
   `SEMBLE_ENABLED` default.
8. Tests (see [Tests](#tests)).
9. Add `changelog.d/<phase-issue-or-pr>-semble-overflow-off.md` with
   `<!-- changelog: changed -->`, following §20.D/E. Include the numbers from
   the Context section.

### Phase 2 — Remove the unused bootstrap from four workflows

1. Delete exactly three steps from each workflow: `setup-uv`
   (`astral-sh/setup-uv@v7` gated on `SEMBLE_ENABLED`), `Install semble` and
   `Build semble index`.
   - `.github/workflows/clarify.yml:704-738`
   - `.github/workflows/plan.yml:1000-1034`
   - `.github/workflows/orchestrate.yml:503-540`
   - `.github/workflows/orchestrate_clarify_respond.yml:602-630`

   Line numbers are as of `main` @ `e3895ed`; re-locate by step name.
2. Keep in each of those workflows: the `SEMBLE_ENABLED` env line, the
   runtime default exports (`SEMBLE_AVAILABLE=false`,
   `SEMBLE_INDEX_PATH=${RUNTIME_DIR}/.semble-index`,
   `SEMBLE_INDEX_AVAILABLE=false`) and the support-file staging lists.
   Before deleting, confirm (grep) that no later step in the same workflow
   reads `SEMBLE_*`, `semble` or `uv`.
3. Rewrite `tests/test_semble_workflow_parity_contract.py`:
   - Keep `TARGET_WORKFLOWS`.
   - Replace the install/order tests with
     `test_target_workflows_do_not_bootstrap_semble`. It asserts that
     `- name: Install semble`, `- name: Build semble index`,
     `bash scripts/install_semble.sh` and `bash scripts/build_semble_wrapper.sh`
     are absent, and that no `astral-sh/setup-uv` step is gated on
     `SEMBLE_ENABLED`.
   - Keep `test_target_workflows_do_not_add_query_calls_yet` and
     `test_target_workflows_stage_revalidate_lifecycle_ai_memory_schemas`.
   - Add `test_target_workflows_keep_fail_open_semble_runtime_defaults`
     (`SEMBLE_INDEX_AVAILABLE=false` and the index-path export still present).
   - Removing `test_target_workflows_define_semble_parity_contract` and
     `test_target_workflows_order_semble_steps_before_codex_config` was
     approved in Q3.
4. Grep `tests/` for `Install semble` and `build_semble_wrapper`, and update
   any other test that pins these four workflows. Known files that mention
   the strings: `tests/test_judge_semble_prefetch_contract.py` and
   `tests/test_orchestrate_poll_process.py` (check whether they target these
   four), `tests/test_workflow_log_analysis_failure_contract.py` (log
   analysis, untouched).
5. `README.md:2850` (Semble rollout note): remove `clarify`, `plan`,
   `orchestrate` and `orchestrate_clarify_respond` from the list of
   workflows carrying install and index steps, and say why: they had no
   query path.
6. Add `changelog.d/<phase-issue-or-pr>-semble-drop-unused-bootstrap.md` with
   `<!-- changelog: changed -->`, citing roughly 30 s saved per clarify,
   plan, orchestrate and clarify-respond run.

### Phase 3 — Lazy bootstrap

1. `scripts/semble_helpers.sh`: add `semble_ensure_ready`, with the opening
   brace on a new line as in the rest of the file. Behaviour:
   - Accept the required target slug from `semble_query_block`, for use in
     bootstrap-failure telemetry (`target=<slug>`).
   - **Mode.** `mode="${SEMBLE_BOOTSTRAP_MODE:-eager}"`, lower-cased. Any
     value other than `lazy` or `eager` logs
     `::warning::SEMBLE_BOOTSTRAP_MODE=<v> is invalid; using eager` once and
     is treated as `eager`.
   - **Return 0 immediately** if `SEMBLE_AVAILABLE=true` and
     `SEMBLE_INDEX_AVAILABLE=true`. That covers the eager path and an
     already-bootstrapped job.
   - **Return 1** if the mode is not `lazy`, or `SEMBLE_ENABLED` is not
     `true`.
   - **State file.** Use the exported `SEMBLE_BOOTSTRAP_STATE_FILE`
     default from Constraints, including run attempt (the implement job's
     `RUNTIME_DIR` contains only the run ID). Write it atomically, holding
     `key=value` lines: `state`, `semble_bin`, `index_path`.
     - If it says `state=ready`, export `SEMBLE_AVAILABLE=true`,
       `SEMBLE_INDEX_AVAILABLE=true`, `SEMBLE_BIN` and `SEMBLE_INDEX_PATH`,
       prepend `dirname SEMBLE_BIN` to `PATH`, and return 0 **only after**
       verifying the recorded index is a nonempty file and the wrapper is
       executable. A stale or invalid ready state must not authorize a query.
     - If it says `state=failed`, return 1. There is no retry within a job.
   - **Bootstrap.** Record a deadline before taking
     `flock -w "${SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS:-180}"` on
     `<state file>.lock`, re-read the state file (another process may have
     finished), then run the remaining work under `timeout` with the
     **remaining** budget, not another full 180 seconds:
      - `bash "<helpers dir>/install_semble.sh"`; the installer deliberately
        returns 0 even on failed installation, so also verify its pinned
        Python module/version matches the installer's configured pin before
        proceeding. Measure this call in the helper's shell and export
        `SEMBLE_INSTALL_MS` for the builder; it does nothing until Phase 4
        adds bootstrap telemetry and makes both merge orders safe.
      - Only after that check succeeds, run
        `SEMBLE_INDEX_PATH="$(_semble_index_path)" bash "<helpers dir>/build_semble_wrapper.sh"`.
        The builder also exits 0 on failure; verify its postconditions.

     Resolve `<helpers dir>` the same way the file already resolves
     `emit_event.sh`: its own directory first, then `scripts/`.
   - **Success** means the index file is nonempty and the wrapper
     `$(dirname <index>)/semble/bin/semble` is executable. The two scripts
     already append to `GITHUB_ENV` and `GITHUB_PATH`, so later steps inherit
     the result. Write `state=ready`, export as above, and return 0.
   - **Install/build/postcondition failure or timeout:** write `state=failed`
     while holding the lock, emit `SEMBLE_FALLBACK
     target=<slug> reason=lazy-bootstrap-failed` via `_semble_log_event`,
     and return 1. On `flock` timeout, return 1 without overwriting another
     process's state; emit the same fallback and allow a later query to retry.
     Never exit non-zero from the caller's perspective beyond the function's
     return code.
2. `scripts/semble_helpers.sh`: add `semble_should_query`. It returns 0 when
   `SEMBLE_INDEX_AVAILABLE=true`, or when the mode is `lazy`,
   `SEMBLE_ENABLED=true` and the state file does not say `state=failed`.
   Otherwise it returns 1.
3. `scripts/semble_helpers.sh` `semble_query_block` (line 122): immediately
   after computing `target="$(_semble_target_slug "${header_label}")"` and
   before the `SEMBLE_AVAILABLE` check, add
   `semble_ensure_ready "${target}" >/dev/null || true`. Stdout must stay clean because
   callers capture it as the prefetch text; **do not suppress stderr**,
   which carries installer errors and Phase 4 bootstrap events. The existing
   fallback events then fire unchanged if Semble is still unavailable.
4. Replace the outer gates with `semble_should_query`, keeping each gate's
   other conditions:
   - `scripts/orchestrate_poll_process.sh:317-318` and
     `scripts/review_rb_judge.sh:713-714`. These are early-`return` forms
     (`if … || [ "${SEMBLE_AVAILABLE:-false}" != "true" ] || [ "${SEMBLE_INDEX_AVAILABLE:-false}" != "true" ] || …; then return 0; fi`).
     Replace the two `SEMBLE_*` conditions with a single negated
     should-query check.
   - `scripts/review_conflict_resolve.sh:2045`
   - `scripts/review_run_reviewers.sh:2983`

   Each must still fail open if `semble_should_query` is undefined (an old
   helpers file staged on a pinned ref). Use an explicit branch: if the
   function exists, use **only** its exit status; otherwise use the legacy
   `SEMBLE_INDEX_AVAILABLE=true` check. Do not write `declare -F ... &&
   semble_should_query || [ "${SEMBLE_INDEX_AVAILABLE:-false}" = "true" ]`:
   its right-hand fallback would override a deliberate false verdict.
   Do **not** change the targeted-file-context overflow guards (see
   cross-phase notes).
5. Workflows. Add top-level
   `SEMBLE_BOOTSTRAP_MODE: ${{ vars.SEMBLE_BOOTSTRAP_MODE || 'lazy' }}` and
   change step conditions:
   - Compare `env.SEMBLE_BOOTSTRAP_MODE != 'lazy'` in workflow step `if:`
     conditions. GitHub string comparison is case-insensitive; this sends
     uppercase `EAGER` and invalid values to eager, matching the helper's
     normalized mode. Test lowercase/uppercase and invalid overrides.
   - `implement.yml:1518-1562`: setup-uv becomes
     `env.SKIP_IMPLEMENT != 'true' && ((env.SEMBLE_ENABLED == 'true' && env.SEMBLE_BOOTSTRAP_MODE != 'lazy') || env.SERENA_ENABLED == 'true')`.
     Install semble and Build semble index use only
     `env.SKIP_IMPLEMENT != 'true' && env.SEMBLE_ENABLED == 'true' && env.SEMBLE_BOOTSTRAP_MODE != 'lazy'`.
     Serena alone must never start a Semble install.
   - `review_autofix.yml:2955-3010`: the same split, replacing
     `env.SKIP_IMPLEMENT` with `env.PR_CLOSED`. Keep the edit minimal for §27.
   - `orchestrate_poll.yml:601-660`: add
     `&& env.SEMBLE_BOOTSTRAP_MODE != 'lazy'` to setup-uv, Install semble
     and Build semble index.
   - `validate.yml:810-930`:
     - In `Determine Semble bootstrap state`, keep `bootstrap_enabled=true`
       when either Serena is enabled **or** `VALIDATION_USE_SEMBLE` is
       truthy and the lower-cased mode is not `lazy`. `setup-uv` keeps this
       gate; gate Install semble separately on `enabled == 'true'` and
       mode not `lazy` (its existing in-step check stays), and Build semble
       index on the same Semble-only condition. Serena still gets uv even
       when Semble is lazy or disabled.
     - When `VALIDATION_USE_SEMBLE` is truthy and the mode is `lazy`, write
       `SEMBLE_ENABLED=true` to `GITHUB_ENV`, so `semble_ensure_ready` can
       bootstrap on demand. The existing runtime defaults
       (`SEMBLE_AVAILABLE=false` and so on) stay.
     - Add the top-level `SEMBLE_BOOTSTRAP_MODE` env line.
   - Confirm `install_semble.sh`, `build_semble_wrapper.sh` and
     `semble_helpers.sh` remain in each workflow's staging list. They do
     today.
6. `README.md`:
   - Add a row for `SEMBLE_BOOTSTRAP_MODE`: default `lazy` in implement,
     review_autofix, orchestrate_poll and validate; `eager` everywhere else.
   - Document the index-freshness difference (Q5): a lazy index reflects the
     workspace at first query, including in-progress edits on repair and
     diagnose.
   - Document that re-enabling overflow needs `SEMBLE_BOOTSTRAP_MODE=eager`.
   - Add rows for `SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS` and
     `SEMBLE_BOOTSTRAP_STATE_FILE` (run-and-attempt-scoped default).
7. Tests (see [Tests](#tests)). Register any new test file in `ci.yml`,
   `test-and-mark-stable.yml` and `mark-stable.yml` wherever sibling Semble
   tests are invoked by explicit path (e.g. `ci.yml:449`, `ci.yml:865`).
8. Add `changelog.d/<phase-issue-or-pr>-semble-lazy-bootstrap.md` with
   `<!-- changelog: changed -->`.

### Phase 4 — Telemetry and echo rejection

1. Run field. Compute `run=` as `"${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT:-1}"`,
   and omit it when `GITHUB_RUN_ID` is unset.
   - `scripts/semble_helpers.sh` `_semble_log_event`: append `run=` to every
     `SEMBLE_QUERY` and `SEMBLE_FALLBACK` line. For fallbacks, add
     `sources=0`; add `static_dup_bytes=0` only if the configured static
     context file is readable. No chunks were injected on a fallback.
   - `scripts/targeted_file_context.py` `_log_semble_event`: do the same.
     The field order after the prefix is free; parsers are key=value.
     Apply the same zero-contribution fallback fields there.
2. `scripts/semble_helpers.sh` `semble_query_block`, on success:
   - `sources=<n>`: the number of distinct paths matching
     `^\[[0-9]+\] (\S+):[0-9]+-[0-9]+` in the returned text.
   - `static_dup_bytes=<n>`, only when `SEMBLE_STATIC_CONTEXT_FILE` is set and
     readable: the sum of byte lengths of returned chunk lines (stripped
     length ≥ 40) that appear verbatim as a line in that file. Compute it
     with an inline `python3 -` heredoc, which is already a Semble
     prerequisite. Fail open: on error, omit the field.
   - Apply the same two fields in `targeted_file_context.py`'s overflow
     emitter, in case overflow is ever re-enabled.
3. `scripts/install_semble.sh`: measure its own elapsed milliseconds and
   write `SEMBLE_INSTALL_MS=<ms>` to `GITHUB_ENV` for the next eager workflow
   step. Re-check uniqueness first. In the lazy helper, measure the elapsed
   install call in the **parent** and export `SEMBLE_INSTALL_MS` before
   invoking the builder as a child (done in Phase 3); a child installer
   cannot export a value to its parent or sibling, and `GITHUB_ENV` is not
   reloaded mid-step.
4. `scripts/build_semble_wrapper.sh`: on both success and `mark_unavailable`,
   emit `SEMBLE_BOOTSTRAP` via `emit_event` and stderr, in the same
   single-line `key=value` shape as `_semble_log_event`, with these fields:
   - `mode=${SEMBLE_BOOTSTRAP_MODE:-eager}`
   - `state=ready|failed`
   - `install_ms=${SEMBLE_INSTALL_MS:-0}`
   - `index_ms=<own elapsed>`
   - `run=`
   - `reason=<sanitised reason>`, on failure only
   If the lazy install fails before the builder starts, emit one failed
   `SEMBLE_BOOTSTRAP` from `semble_ensure_ready` with the measured install
   time and `index_ms=0`. Never run the builder just to generate an event.
   In the eager `review_autofix.yml` and `orchestrate_poll.yml` index steps,
   the builder's stderr is redirected to `${RUNTIME_DIR}/semble_index.log`.
   After the builder returns, print only its `SEMBLE_BOOTSTRAP` line from
   that file on both success and failure; leave the existing failure-tail
   diagnostic unchanged and do not print the full log. Otherwise eager
   bootstraps in those workflows are invisible to log-based aggregates.
5. Callers set `SEMBLE_STATIC_CONTEXT_FILE` where the same prompt embeds a
   static prefix that exists at query time:
   - `implement.yml` (both repair and diagnose): `./pre_assembled_static.txt`
   - `scripts/orchestrate_poll_process.sh` judge paths: pass the static file
     actually used by each prompt to `render_judge_semble_prefetch_from_query_file`,
     which sets the variable locally around `semble_query_block` (line 327)
     only when that file is readable and nonempty. Assemble
     `${RUNTIME_DIR}/judge_static.txt` before prefetch in the main,
     security-pass exhaustion and stall judge paths (currently assembled
     afterward). In the review-blocked path, move prefetch after its existing
     branch preparation and static assembly, so the measured file still
     matches the prompt after checkout. The integration-conflict judge already
     assembles its separate `${judge_static_file}` before prefetch; pass that
     file rather than `${RUNTIME_DIR}/judge_static.txt`. On assembly failure,
     leave the variable unset so `static_dup_bytes` is omitted, not guessed.
   - `review_autofix.yml`: the static file the reviewer and editor prompts
     embed. Locate it via `pre_assembled_static.txt` handling in
     `review_run_reviewers.sh` / `review_apply_fixes.sh`; if none exists at
     query time, leave the variable unset.
6. `agents.md`: add `SEMBLE_BOOTSTRAP` to both LOG_PREFIX registries, after
   `SEMBLE_FALLBACK`: the bullet list near line 1333 and the
   `LOG_PREFIX.name=` list near line 1535.
7. `scripts/cost_audit.py`:
   - `_validated_mcp_telemetry_event`: recognise `SEMBLE_BOOTSTRAP`. It
     requires `mode`, `state`, and numeric `install_ms` and `index_ms`.
   - `parse_log(log, *, fallback_wall_clock_ms=None)` gains an optional
     `run_key: str | None = None` (additive). When it is set, drop any
     `SEMBLE_*` or `SERENA_*` telemetry line whose `run=` is present and
     differs, counting it in `semble_echo_lines_dropped`. Lines without
     `run=` are accepted as today.
   - Extend the existing `list_runs` `gh run list --json` field list with
     `attempt`, and pass
     `run_key=f"{r['databaseId']}-{r['attempt']}"` from the run loop.
     Confirm the pinned `gh` supports `attempt` when implementing; if an
     attempt is absent or invalid, omit `run_key` and report the uncertainty
     rather than guessing attempt 1 and dropping legitimate rerun events.
     This extends one existing request; it does not add a per-run API call.
   - Forward the optional `run_key` through `build_run_cost_telemetry` to
     `parse_log`, preserving the default for existing callers.
   - New aggregates in `build_run_cost_telemetry` and
     `aggregate_run_cost_telemetry`, all additive keys:
     - `semble_bootstraps`
     - `semble_bootstraps_failed`
     - `semble_bootstraps_unused` (a bootstrap in a run with zero runtime
       `SEMBLE_QUERY`)
     - `semble_bootstrap_ms_total`
     - `semble_sources_total`
     - `semble_static_dup_bytes_total`
     - `semble_echo_lines_dropped`
   - Surface them in the markdown table.
8. `scripts/collect_workflow_logs.py`: apply the same `run=` mismatch drop
   where it validates retained telemetry lines, using the existing `run_id`
   **and** `run_attempt` of the run it is collecting. Recognise
   `SEMBLE_BOOTSTRAP` as structured telemetry; pass that same run key to
   `build_run_cost_telemetry` from `_apply_cost_telemetry_from_full_logs`
   so the second parsing path cannot re-count a dropped echo.
9. Add `SEMBLE_BOOTSTRAP` to the listed telemetry prefixes in **both** prompt
   bodies, so the log-analysis model reads it:
   - the legacy body `prompts/mode-workflow-analysis.txt` (lines ~23 and
     ~92);
   - the shared-prelude template `prompts/_templates/mode-workflow-analysis.txt`
     (lines ~16 and ~85).

   Both bodies are maintained side by side (README "Phase O shared-prelude
   rollout"). Keep the prompt parity tests (`tests/test_assemble_prompt.py`
   and related) green.
10. Add `changelog.d/<phase-issue-or-pr>-semble-telemetry.md` with
    `<!-- changelog: added -->`.

## Files & Modules

- `.github/workflows/implement.yml` (P1, P3, P4)
- `.github/workflows/review_autofix.yml` (P1, P3, P4; §27 headroom)
- `.github/workflows/orchestrate_poll.yml` (P1, P3, P4)
- `.github/workflows/validate.yml` (P3)
- `.github/workflows/clarify.yml` (P2)
- `.github/workflows/plan.yml` (P2)
- `.github/workflows/orchestrate.yml` (P2)
- `.github/workflows/orchestrate_clarify_respond.yml` (P2)
- `scripts/semble_helpers.sh` (P3, P4)
- `scripts/targeted_file_context.py` (P4 only; no Phase 1 change)
- `scripts/install_semble.sh` (P4)
- `scripts/build_semble_wrapper.sh` (P4)
- `scripts/review_apply_fixes.sh` (P1)
- `scripts/review_run_reviewers.sh` (P1, P3)
- `scripts/review_conflict_resolve.sh` (P1, P3)
- `scripts/review_rb_judge.sh` (P3)
- `scripts/orchestrate_poll_process.sh` (P3, P4)
- `scripts/cost_audit.py` (P4)
- `scripts/collect_workflow_logs.py` (P4)
- `prompts/_templates/mode-workflow-analysis.txt`, `prompts/mode-workflow-analysis.txt` (P4)
- `README.md` (P1, P2, P3)
- `probably_unnecessary_but_read_if_stuck.md` (P1)
- `agents.md` (P4)
- `tests/test_semble_workflow_parity_contract.py` (P2)
- `tests/test_implement_semble_contract.py` (P1, P3)
- `tests/test_review_semble_contract.py` (P1, P3)
- `tests/test_validate_semble_contract.py` (P3)
- `tests/test_judge_semble_prefetch_contract.py` (P3, if it pins the poller gate)
- `tests/test_semble_helpers.py` (P3, P4)
- `tests/test_targeted_file_context.py` (P4)
- `tests/test_cost_audit_semble_metrics.py` (P4)
- `tests/test_collect_workflow_logs.py` (P4)
- `changelog.d/<phase-issue-or-pr>-semble-overflow-off.md` [new] (P1)
- `changelog.d/<phase-issue-or-pr>-semble-drop-unused-bootstrap.md` [new] (P2)
- `changelog.d/<phase-issue-or-pr>-semble-lazy-bootstrap.md` [new] (P3)
- `changelog.d/<phase-issue-or-pr>-semble-telemetry.md` [new] (P4)

## Data Model / Index Changes

None. No MongoDB collection, index or `/db/contracts/*` file is touched
(§10 N/A).

## Tests

**Phase 1 (unit and contract).**
- Update `tests/test_implement_semble_contract.py::test_targeted_file_context_receives_semble_inputs`
  (line 195). Assert the `--semble-*` flags are appended only inside the
  `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED` guard, and that the env
  default is `'false'`.
- Update `tests/test_review_semble_contract.py::test_editor_targeted_file_context_and_prompt_render_path_passes_flags`
  (line 490) and `::test_conflict_prepare_and_resolve_wire_semble_query_and_prompt_append`
  (line 588) the same way.
- Add a check for the reviewer-scope guard at `review_run_reviewers.sh:2080`.
- Add `test_overflow_switch_defaults_false_in_reusable_workflows`, covering
  implement, review_autofix and orchestrate_poll.
- Behavioural test: run `scripts/review_apply_fixes.sh`'s argument-building
  block (or `targeted_file_context.py` directly with and without the flags)
  over a fixture whose file exceeds `--max-bytes`. Assert the output contains
  the `would overflow total budget` marker and no `chunk-retrieved via semble`
  header when the switch is unset.
- Existing `tests/test_targeted_file_context.py` stays green unchanged (the
  Python flags are preserved).

**Phase 2 (contract).** The rewritten
`tests/test_semble_workflow_parity_contract.py`, plus the YAML parse of the
four workflows. Run the full `pytest tests/` to catch any other test pinning
those steps.

**Phase 3 (unit).** New cases in `tests/test_semble_helpers.py`, using stub
`install_semble.sh` and `build_semble_wrapper.sh` in a temp helpers dir that
write a fake index and wrapper and count invocations:
- lazy ready path
- `semble_query_block` inside `$(...)` triggers exactly one bootstrap, and a
  second call reads the state file without re-installing
- two concurrent calls (`&` plus `wait`) produce exactly one install
- failure writes `state=failed`, emits `reason=lazy-bootstrap-failed`, and
  does not retry; assert the event's `target=` matches the caller's label
- a fail-soft installer returning 0 without a pinned module skips the build;
  a fail-soft builder returning 0 without a nonempty index or executable
  wrapper never writes `state=ready`
- a lock timeout never overwrites another installer's ready state
- unset or `eager` mode never installs
- an invalid mode warns and acts as eager
- workflows with Serena enabled still set up uv in lazy mode but do not run
  either Semble bootstrap step; uppercase and invalid modes match helper mode
- the timeout path, with a stub that sleeps past
  `SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS=1`
- `semble_should_query` truth table

Contract updates:
- `tests/test_implement_semble_contract.py::test_semble_bootstrap_steps_are_gated_and_fail_open`
  (line 159)
- `tests/test_review_semble_contract.py::test_workflow_adds_gated_setup_install_index_and_editor_only_serena_steps`
  (line 273). Serena's setup-uv path must remain.
- `tests/test_validate_semble_contract.py` (mode-aware `semble_gate`)
- The poller and judge gate tests

**Phase 4 (unit).**
- `tests/test_semble_helpers.py`: `run=`, `sources=` and `static_dup_bytes=`
  formatting and omission rules, including measured zero on fallbacks and
  lazy install failure events reaching stderr.
- Judge prefetch contract tests: each judge path passes its actual static
  file after assembly (and after branch prep in the review-blocked path), or
  omits the field when the static file is unavailable.
- `tests/test_targeted_file_context.py`: the same fields on the overflow
  emitter.
- `tests/test_cost_audit_semble_metrics.py`:
  - `SEMBLE_BOOTSTRAP` validation, including malformed lines being ignored
  - unused-bootstrap detection
  - the `run_key` mismatch drop
  - the `databaseId`/`attempt` run-list field contract and reruns
  - lines without `run=` being accepted
  - aggregate keys
- `tests/test_collect_workflow_logs.py`: the same drop rule and
  `SEMBLE_BOOTSTRAP` retention through both log retention and the
  `build_run_cost_telemetry` reparse path.
- Contract tests for `review_autofix.yml` and `orchestrate_poll.yml`: the eager
  builder's redirected log contributes one `SEMBLE_BOOTSTRAP` line to the job
  log on both successful and failed builds, without replaying the whole log.
- Prompt parity tests for the workflow-analysis template.

**Every phase.**
- `PYTHONDONTWRITEBYTECODE=1 pytest tests/`
- `bash -n` on touched scripts
- `actionlint` / YAML parse on touched workflows
- `wc -c .github/workflows/review_autofix.yml` < 480,000

**Post-merge verification (automated, no operator step).** The existing
scheduled `workflow-log-analysis.yml` reports and `scripts/cost_audit.py`
read the new events. Expected signals:
- `SEMBLE_QUERY target=overflow` count 0 (P1)
- no `install_semble:` lines in clarify, plan, orchestrate or
  orchestrate_clarify_respond (P2)
- pollers' `semble_bootstraps` close to the number of ticks with judge
  queries (P3)
- `semble_bootstraps_unused` close to 0 in lazy workflows (P3/P4)

## Risks & Mitigations

- **Lazy bootstrap adds 30–35 s at the first query point inside a step**
  (reviewer stage, poller judge, validate discover).
  - Mitigation: those steps already allow multi-minute runtimes. The
    bootstrap is bounded by `SEMBLE_LAZY_BOOTSTRAP_TIMEOUT_SECS=180` and
    fails open.
  - It removes the same cost from every job that never queries.
  - Poller ticks (p50 ≈ 250 s against a 300 s schedule) get faster on the
    majority of ticks.
- **Exports made inside `$(...)` subshells are lost.** Mitigation: the state
  file plus re-export on read; covered by a Phase 3 test.
- **Concurrent first queries race.** Mitigation: `flock` on
  `<state file>.lock` with a re-check after acquiring it; the exported default
  is scoped to the run and attempt, and a lock timeout cannot overwrite the
  holder's state. Covered by tests.
- **An eager bootstrap failure must not trigger a lazy retry in workflows
  left eager** (workflow-log-analysis, consumers' other paths). Mitigation:
  the helper defaults to `eager` when `SEMBLE_BOOTSTRAP_MODE` is unset, so
  `semble_ensure_ready` returns 1 there.
- **The lazy index may include in-progress edits.** ACCEPTED (Q5: A),
  documented in README.
- **Re-enabling overflow in lazy mode does nothing unless Semble was already
  bootstrapped.** ACCEPTED: documented. Re-enabling needs both repo
  variables, `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED=true` and
  `SEMBLE_BOOTSTRAP_MODE=eager`.
- **Old helpers staged on a pinned ref lack `semble_should_query`.**
  Mitigation: gates use the legacy index check only if the helper is absent;
  a false helper result is never overridden.
- **The `run=` filter cannot catch echoes of a run's own lines inside the
  same run.** ACCEPTED: documented limitation. Cross-run echoes, the
  failure mode observed in the audit, are caught.
- **`static_dup_bytes` is a line-overlap heuristic, not exact token
  accounting.** ACCEPTED: named and documented as a heuristic in README and
  the changelog fragment.
- **`review_autofix.yml` §27 headroom (~34.6 KB).** Mitigation: Phases 1, 3
  and 4 add only env lines and condition edits there. Each phase PR checks
  `wc -c`. If any phase would cross 480,000 bytes, move an inline `run:` body
  to `scripts/` per §27 in that same PR.
- **Consumer repos pinned to `@stable` see nothing until the next tag.**
  ACCEPTED: standard §14 propagation. Consumers that set
  `vars.SEMBLE_ENABLED=false` are unaffected.

## Rollout

- Each phase ships on merge to `main` and reaches consumers on the next
  `@stable` tag (§14). No migration and no operator step.
- **Kill switches (repo variables, no code revert needed):**
  - Phase 1: `TARGETED_FILE_CONTEXT_SEMBLE_OVERFLOW_ENABLED=true` restores the
    overflow lookup. Phase 3 being merged additionally needs
    `SEMBLE_BOOTSTRAP_MODE=eager`.
  - Phase 3: `SEMBLE_BOOTSTRAP_MODE=eager` restores eager install and index
    in all four workflows.
  - Phase 2 and Phase 4: revert the PR. Phase 4 is purely additive
    telemetry.
- **Recommended merge order** (not required):
  1. Phase 1 (removes the harmful context).
  2. Phase 2.
  3. Phase 4 (gives a baseline before lazy mode).
  4. Phase 3.

## References

- Audit session findings (2026-10-05): the overflow replay on issue #6235's
  plan; 112-run sample covering 2026-07-10 to 2026-09-24.
- Issue #3990 / run 33796624872: Semble overflow blocks overflowed codex's
  stdin cap.
- Implement run 37245942197: identical 7,560-byte overflow blocks for four
  different files.
- #2445 / 04de32ea1f8: Semble rollout. #2446 / 70654aa2cf9: `SEMBLE_ENABLED`
  default flipped to `'true'`.
- `analysis/workflow-optimization-2026-09-03.md` to
  `analysis/workflow-optimization-2026-10-03-2.md`: repeated "value
  unproven" and "lazy-install Semble in pollers" recommendations.
- `tests/test_semble_workflow_parity_contract.py:86`: "should not add Semble
  query wiring yet".
- CLAUDE.md §4, §5, §6, §9, §14, §15, §18, §20, §27.
