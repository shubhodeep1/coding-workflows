# Fold the small per-PR checks into one job

## Summary

Three small workflows in coding-workflows each take their own runner on a pull request event for under a minute of work: `Integration PR readiness check`, `Lint PR body for auto-close keywords against orchestrator-tracking issues` and `Lint plan-archival completeness`. This plan folds them into one job in one workflow, keeps the readiness status context, adds the folded job's name `pr-body-lint` to the merge gate's default list in the same PR, and narrows the `push` trigger of the consumers' `AI Workflow Integrity` wrapper. It is two independently mergeable PRs.

## §18.E Automation

- No new script, supervisor or scheduled workflow. Phase 1 adds one workflow file that replaces three and extends existing scripts (`scripts/pr_checks_lib.sh`, `scripts/orchestrate_poll_process.sh`: one gate-list entry); Phase 2 edits one template.
- Entry points: `.github/workflows/pr-checks.yml` (`pull_request` into `main` and `stable`); `workflow-templates/ai-workflow-integrity.yml` (`pull_request_target` and `push`), delivered by `.github/workflows/update_workflows.yml`.
- No long-running supervisor is needed.
- No DB work.
- `docs/scripts-pending-removal.md`: no entry added or removed.

## Context

Audit of issues, PRs and Actions runs for 2026-11-10 to 2026-11-17 (eight days):

| The numbers that matter | Value |
| --- | --- |
| `Integration PR readiness check` runs, coding-workflows | 1,192 |
| `Lint PR body for auto-close keywords…` runs, coding-workflows | 1,015 |
| Work per run | under 1 minute |
| Queue wait per run | 20 to 70 minutes |
| `AI Workflow Integrity` runs in three consumers (push plus `pull_request_target`) | 850 / 809 / 265 |
| `Updater Gate Check` runs in `binance-blessings` | 447 |

Each of these runs holds one slot of the account's shared runner pool (CLAUDE.md §33). The work is small; the cost is the slot and the queue wait.

What the code says today (read on `main` at 88de9d5 and re-checked against this branch; `main` has since moved, so Phase 1 step 0 re-verifies every bullet below on the current tip before any edit):

- **Check-run names are job names, not workflow names.** PR #9636's head carries check-runs named `readiness` and `lint`; none is named after a workflow. `_pr_checks_completed` (`scripts/pr_checks_lib.sh`) matches a failed check-run by `.name` against the required set, and counts only check-runs that exist: a listed name with no check-run blocks nothing. So the entries `Integration PR readiness check`, `Lint plan-archival completeness` and `Lint PR body for auto-close keywords against orchestrator-tracking issues` in `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` match no check-run.
- **Three jobs are named `lint`.** `ci.yml` (CI's aggregate job), `lint-pr-body-auto-close.yml` and `lint-plan-archival.yml` each have a job with id `lint` (the `_mark_superseded` comment in `scripts/pr_checks_lib.sh` says so). No job is named `pr-body-lint` yet, and `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` (`scripts/pr_checks_lib.sh` and `scripts/orchestrate_poll_process.sh`, pinned equal by `tests/test_pr_checks_lib_required_filter.py`) does not list it. A failed PR-body lint and a failed plan-archival lint both block through the entry `lint`, the name they share with CI's aggregate job.
- **The gate's treatment of same-name check-runs.** `skipped`, `success` and `neutral` are acceptable. Only a `cancelled` run can be superseded by a newer run of its name; a failed, timed-out or running check-run blocks on its own even when a newer run of the same name exists.
- **The readiness verdict is a commit status, not the job result.** `scripts/check_integration_pr_readiness.py` posts `orchestrator/integration-pr-not-ready` and exits 0 for ready and not-ready alike. It exits 1 when it cannot post the status, and also when it cannot fetch the tracking issue, even after posting an `error` status for that (lines 202-207). Today either failure fails only the `readiness` check-run, which no `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` entry and no ruleset entry names, so it blocks no merge. The orchestrator refreshes the same status itself when a tracking issue's checkboxes change (`TRACKING_BODY_READINESS_REFRESH` in `scripts/orchestrate_poll_process.sh`).
- **The PR-body and plan-archival verdicts are job results.** Both scripts exit non-zero on a violation.
- **All three run on `pull_request`** into `main` or `stable`, from the PR's merge ref, with `github.token` and no secret. Their permissions differ only in `statuses: write` (readiness).
- **`lint-plan-archival.yml` is path-filtered** (`docs/completed/**`) and still has a job named `lint`, the name the `main` ruleset requires and means CI by.
- **The `main` ruleset** (`Required checks for default branch`, id 25851281) requires `lint` (GitHub Actions app) and `ai-review/head-gate`. It lists neither `pr-body-lint` nor `orchestrator/integration-pr-not-ready`. Repository admins can bypass it.
- **`AI Workflow Integrity`** (`workflow-templates/ai-workflow-integrity.yml`) runs on `pull_request_target` and on every branch push. Only the pull request run posts the required status `coding-workflows/workflow-integrity`. A push run posts nothing and ends at `no_managed_wrapper_changed` unless a managed wrapper changed.
- **`Updater Gate Check`** is `binance-blessings`' own file (`.github/workflows/updater-gate-check.yml`). coding-workflows does not ship it.

An earlier session tried the fold directly and stopped on three points. This plan answers each:

- The merge gate list names two of the workflows. Those entries match nothing (first bullet), so removing the workflows changes nothing there. The lint verdicts block through `lint` today; the folded job gets a new id (`pr-body-lint`, OD1), so Phase 1 adds that name to `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` in the same PR and keeps `lint`, which CI owns.
- `Internal: Issue-PR Status Sync` runs on `closed` only, with `secrets: inherit` and `issues: write`. It stays out (Non-goals).
- `AI Workflow Integrity` runs on `pull_request_target`. It is never merged with a `pull_request` job; only its `push` trigger changes.

## Goals

- One runner job per `opened`, `synchronize`, `reopened` and `ready_for_review` event does the work of the readiness check, the PR-body lint and, when the PR changes `docs/completed/**`, the plan-archival lint.
- A failed PR-body lint, a failed plan-archival lint and a not-ready integration PR block exactly the merges they block today.
- No pull request that is open when the fold merges is left waiting for a check that can no longer arrive.
- No check-run named `lint` comes from any workflow other than `ci.yml`.
- A consumer's `AI Workflow Integrity` starts on a push only when the push changes a file under `.github/workflows/`.
- The user changes nothing by hand for the fold to be safe.

## Non-goals

- `CI quick` (`.github/workflows/ci_pr_quick.yml`) stays its own workflow. It triggers on PRs into every base, not only `main` and `stable`; it has `cancel-in-progress: true`, which must not cancel a status write; and its caller permissions include `pull-requests: write`, `issues: write` and `actions: write`. Folding the small checks into it would also make every body edit and label change start the 11-job reusable `ci.yml` call.
- `Internal: AI Review & Autofix`, `Internal: Issue-PR Status Sync` and `Internal: Cancel on PR close` stay as they are. They run with secrets through reusable workflows; the three folded checks run PR-controlled code with a read-mostly job token (§33.A, isolation).
- The `pull_request_target` run of `AI Workflow Integrity`. The consumer ruleset requires its status on every PR head, so it cannot be path-filtered, and it must keep running from the base branch.
- `Updater Gate Check` in `binance-blessings`. It is that repository's own workflow and was not readable from the planning session (open decision OD4).
- Removing the three workflow-name entries from `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` (open decision OD3).
- Making `pr-body-lint` or `orchestrator/integration-pr-not-ready` a required check in the `main` ruleset (open decision OD5). The fold works either way.

## Constraints

- §6: the workflow files, workflow names and job ids that go away are removals. The user approved the fold as a planned change (Q10: A); the exact removals are listed under Names that change, and OD1 and OD2 ask for the two that go beyond the question as asked. The status context `orchestrator/integration-pr-not-ready`, the check-run name `lint` (CI's), every existing `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` entry, the three scripts and their CLI flags, and every log prefix stay. The check-run name `pr-body-lint` and its gate-list entry are additions.
- §19: the PR-body lint is a load-bearing check. Its steps move verbatim, and its failure must fail the job.
- §33.A: fold before adding; say why anything stays separate; coalesce superseded runs; set the merged job's `timeout-minutes` to the sum plus headroom.
- §33.B: jobs per event before and after are stated below.
- §27: the new workflow file is about 12,000 bytes. No file comes near 480,000.
- §14: the Phase 2 template reaches consumers on the next `@stable` sync.
- §15: the fold adds no API call. The plan-archival file list comes from the checkout, not from the API.
- §20 / §30: each PR carries one `changelog.d/` fragment; new agents.md text goes through `agents.d/`; wrong existing text is corrected in place.

## Decisions

Nobody was available to answer questions while this plan was written. D1 to D7 are clear-winner assumptions (CLAUDE.md §2). OD1 to OD5 are open: each carries a recommendation, and Phase 1 is written for the recommended answer. An answer other than the recommendation changes only the lines named under that decision.

### D1 — Host file

- **Chosen:** a new file `.github/workflows/pr-checks.yml`, workflow name `PR checks`. The three old workflow files are deleted in the same PR.
- **Alternatives considered:** keep one of the old files as the host.
- **Why:** each old file's name and header describe one check. Hosting three checks in `lint-pr-body-auto-close.yml` would mislead every later reader.

### D2 — One job, three step groups

- **Chosen:** one job. Each check's steps run under `if: ${{ !cancelled() && <its own condition> }}`, so a failure in one check does not stop the others, and the job fails when any lint step failed. The step "Check integration PR readiness and post status" has `continue-on-error: true`: its verdict is the commit status (D3), and a script exit 1 (status post or tracking-issue fetch failed) must not fail the gated `pr-body-lint` check-run, because today that exit fails only the ungated `readiness` check-run. The step still shows as failed in the run log.
- **Alternatives considered:** three jobs in one workflow.
- **Why:** three jobs in one workflow still take three runners. The checks need no isolation from each other: same trigger, same token, same trust.

### D3 — The readiness verdict stays a commit status

- **Chosen:** `orchestrator/integration-pr-not-ready` is posted exactly as today, by the same script with the same arguments.
- **Alternatives considered:** make the folded job's result carry it.
- **Why:** the orchestrator refreshes that status from the poller, and one job result cannot carry two verdicts.

### D4 — Permissions are the union

- **Chosen:** `contents: read`, `issues: read`, `pull-requests: read`, `statuses: write`.
- **Alternatives considered:** keep the lint steps in a job without `statuses: write`.
- **Why:** a `pull_request` workflow runs the PR's own copy of the workflow and scripts, so a same-repository PR can already use `statuses: write` today through the readiness workflow, and a fork PR's token is read-only whatever the file says. The union gives no principal anything it does not have.

### D5 — Coalescing

- **Chosen:** a `concurrency:` group per pull request with `cancel-in-progress: false`, used only by runs whose job will run. A run whose job is skipped (a label event for a label other than `ai:override-incomplete-merge`) gets a group of its own.
- **Alternatives considered:** no group, as today; one group for every run.
- **Why:** GitHub keeps one running and one waiting run per group and drops an older waiting run. One group for every run would let a skipped label run replace a waiting real run. With the split group, only a newer real run replaces a waiting one, and every real run performs all checks for the PR's current head, so nothing is lost. `cancel-in-progress` stays false because the job writes a status (§33.A).

### D6 — Every real run performs every check

- **Chosen:** on `edited`, the readiness steps run too; on an override-label event, the lint steps run too.
- **Alternatives considered:** skip a check on the events that did not start it before.
- **Why:** D5 depends on it, and all three checks finish in well under a minute.

### D7 — Consumer push filter

- **Chosen:** `paths: ['.github/workflows/**']` on the `push` trigger of `workflow-templates/ai-workflow-integrity.yml`.
- **Alternatives considered:** push on the default branch only; drop the push trigger.
- **Why:** the wrapper must be byte-identical in every consumer, so it cannot name a default branch. Every managed wrapper is a top-level file under `.github/workflows/`, and the reusable workflow already ends at `no_managed_wrapper_changed` for any other push, so the filter removes only runs that do nothing.

### OD1 — Name of the folded job (open)

Today the lint verdicts are carried by jobs named `lint`. The folded job must not be named `lint` (Goals), so whatever its id, that id is new and must be added to `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` in the same PR; otherwise `_pr_checks_completed` ignores a failed folded lint and the §19 check stops blocking the pipeline's merges.

- **Recommended: A — job id `pr-body-lint`.** It names the check with consequences (§19). Phase 1 adds `pr-body-lint` to the default in `scripts/pr_checks_lib.sh` and `scripts/orchestrate_poll_process.sh` (kept identical), the README row, and the tests that pin the string. `lint` stays in the list: it is CI's aggregate job, and heads from before the fold still carry the old lints under that name.
- **B — job id `pr-checks`.** The name fits the content better; the same gate-list addition is made with `pr-checks`.
- **Rejected — keep the id `lint`.** It keeps the duplicate `lint` that can satisfy the ruleset's `lint` requirement before CI finishes (PR #9570).
- **If B:** read `pr-checks` for `pr-body-lint` in Implementation steps 1 and 6 and in Tests.

### OD2 — Fold the plan-archival lint (answered: A)

- **Answer:** A, implemented by PR #9709. `lint-plan-archival.yml` is removed and its steps are the plan-archival step group of the `pr-body-lint` job in `.github/workflows/pr-checks.yml`.
- **Recommended: A — fold it.** It removes `lint-plan-archival.yml` and with it the last job outside CI named `lint`, which today can satisfy the ruleset's `lint` requirement on a PR that changes `docs/completed/**` (the defect PR #9570 showed for the PR-body lint's `lint`). A failed plan-archival lint then blocks through `pr-body-lint`, which Phase 1 adds to the gate default (OD1).
- **B — leave it.** Fewer files change (`.claude/commands/audit-plans.md`, `.claude/commands/implement-plan-ai.md` and their `workflow-templates/.claude/commands/` copies cite the file), and its runs are rare because of the path filter. The duplicate `lint` stays.
- **If B:** drop the plan-archival steps from step 1 and the plan-archival rows from Names that change; the guard test in step 7 keeps `lint-plan-archival.yml` in its allowlist.

### OD3 — Dead gate list entries (open)

- **Recommended: A — keep the three workflow-name entries and correct the README prose.** They match nothing, so keeping them changes nothing, and removing a documented default is a §6 change with no benefit.
- **B — remove them** from both scripts, the README row and the two tests that pin the string.

### OD4 — `Updater Gate Check` in `binance-blessings` (open)

- **Recommended: A — a separate issue in `binance-blessings`,** written after reading `.github/workflows/updater-gate-check.yml` in a session that has that repository attached. 447 runs in eight days in one repository suggest a trigger filter or a fold into that repository's own PR workflow, but the file was not read for this plan.
- **B — leave it.**

### OD5 — Ruleset entries for the folded checks (open)

- **Recommended: B — leave the ruleset as it is.** The pipeline's merges are blocked by its own gate (OD1); a manual merge or an armed auto-merge is not, as today. The reason not to require `pr-body-lint`: the folded job is skipped on every label event for a label other than `ai:override-incomplete-merge`, and each skip adds a `pr-body-lint` check-run with conclusion `skipped` to the head. GitHub reports a job skipped by a conditional as Success and counts it as passing a required check ([Troubleshooting required status checks](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks)), so a label change after a failed lint could satisfy a required `pr-body-lint` that never ran on that event. The pipeline's gate is not affected: `skipped` is acceptable there, and a failed run blocks on its own whatever newer same-name runs exist (Context).
- **A — require `pr-body-lint` in the `main` ruleset after Phase 1.** Safe only once no event can produce a skipped `pr-body-lint`, for example by moving the label filter from the job-level `if:` into the steps (every label event then takes a runner, which undoes `docs/plans/reduce-actions-run-volume-plan.md` item 10). Not part of this plan.
- Whether `orchestrator/integration-pr-not-ready` should be required as well is the same kind of decision. The readiness workflow's header says to require it; the ruleset does not. This plan does not depend on it.

## Approach

### What folds and what stays

| Workflow today | After | Why |
| --- | --- | --- |
| `Integration PR readiness check` (`integration-pr-readiness.yml`, job `readiness`) | step group in `PR checks` | same trigger, token and trust as the lints |
| `Lint PR body for auto-close keywords…` (`lint-pr-body-auto-close.yml`, job `lint`) | step group in `PR checks`, job id `pr-body-lint` (OD1), added to the gate default | carries the verdict; must not be named `lint` |
| `Lint plan-archival completeness` (`lint-plan-archival.yml`, job `lint`) | step group in `PR checks`, runs when the PR changes `docs/completed/**` (OD2) | removes the duplicate `lint` |
| `CI quick` | separate | other bases, `cancel-in-progress: true`, wider permissions, heavier work |
| `Internal: AI Review & Autofix` | separate | secrets, `ai-review/head-gate` |
| `Internal: Issue-PR Status Sync`, `Internal: Cancel on PR close` | separate | `closed` only, `secrets: inherit` |
| `AI Workflow Integrity` (consumers) | separate; `push` trigger path-filtered | `pull_request_target`, required status |
| `Updater Gate Check` (`binance-blessings`) | out of scope (OD4) | not shipped from here |

### The folded workflow

`.github/workflows/pr-checks.yml`:

- `on: pull_request`, `types: [opened, edited, synchronize, reopened, ready_for_review, labeled, unlabeled]`, `branches: [main, stable]`. This is the union of the three trigger lists. No `paths:` filter, because the PR-body lint and the readiness status are needed on every PR.
- Job `pr-body-lint`, `timeout-minutes: 10` (three 5-minute jobs whose real work is under a minute each).
- Job-level `if:`, taken from the readiness workflow: run unless the event is `labeled` or `unlabeled` for a label other than `ai:override-incomplete-merge`. A skipped job takes no runner, but it still adds a `pr-body-lint` check-run with conclusion `skipped` to the head. The pipeline's gate accepts `skipped` and still blocks on any earlier failed `pr-body-lint` on that head (Context), so a skip cannot mask a failure there; GitHub's ruleset would count the skip as passing, which is why OD5 recommends B.
- `concurrency:` per D5. The group expression repeats the job-level condition: `pr-checks-<PR number>` when the job will run, `pr-checks-skip-<run id>` otherwise.
- One checkout with `fetch-depth: 2`. The merge commit's first parent is the base tip, so `git diff --name-only --diff-filter=A HEAD^1 HEAD` lists the files the PR adds without full history and without an API call. If `HEAD` is not a two-parent merge commit, the step fails closed (the plan-archival steps run and the job fails with a clear message) rather than skipping.
- Step groups, each step with its original `name:` and body:
  1. PR-body lint: "Write PR title and body to file", "Write PR commit messages to file" (keeps its `base.ref == default_branch` condition), "Run auto-close keyword lint".
  2. Readiness: "Write PR labels to file", "Check integration PR readiness and post status".
  3. Plan-archival: a new step "Detect docs/completed changes" sets an output; "Write PR body to file", "Enumerate added files in PR" and the lint step run only when it is true. The enumerate step uses the merge-commit diff above in place of `"${BASE_SHA}" "${HEAD_SHA}"` and keeps git's default rename detection, as `lint-plan-archival.yml` does today, so a pure move into `docs/completed/**` is still not listed as added and the linted set does not change.
- Every step after the checkout has `if: ${{ !cancelled() && … }}` (D2).
- The header comment keeps the incident context of all three old headers (postmortem P1, P3 and P6 of `docs/postmortems/2026-05-18-project-2734-stall.md`, PR #7190, PR #9570) (§27: comments that carry incident context are not deleted).

### Names that change, and every reader

| Name | Change | Readers to update |
| --- | --- | --- |
| File `.github/workflows/lint-pr-body-auto-close.yml`, job id `lint` | deleted | `CLAUDE.md` §19 (enforcement item 1; there is no `workflow-templates/CLAUDE.md`, do not create one); `unattended_system_instructions.md` §21 if it names the file; `agents.md` (the §19 paragraph, the two `_pr_checks_completed` paragraphs, the TREE block through `make generate`); `docs/INVENTORY.md`; `scripts/pr_checks_lib.sh` comments; `tests/test_lint_pr_body_auto_close.py`; `tests/test_pr_base_freshness_gate.py` and `tests/test_pr_checks_lib_required_filter.py` docstrings; `tests/test_workflow_checkout_integration_ref_audit.py` registry |
| Workflow name `Lint PR body for auto-close keywords against orchestrator-tracking issues` | no run carries it any more | `CLAUDE.md` §19; README `ORCH_FINAL_MERGE_REQUIRED_CHECKS` row (prose); the gate list entry stays (OD3) |
| The README `AUTO_MERGE_REQUIRED_CI_CHECK` row's sentence that `lint-pr-body-auto-close.yml` and `lint-plan-archival.yml` also have a job called `lint` | stale once both files are deleted | `README.md` (corrected in place in step 5: only `ci.yml` has a job called `lint`) |
| File `.github/workflows/integration-pr-readiness.yml`, job id `readiness` | deleted | `tests/test_check_integration_pr_readiness.py` (loads the file and reads `jobs.readiness.if`); `tests/test_workflow_checkout_integration_ref_audit.py`; `docs/INVENTORY.md`; `agents.md` TREE block; `agents.d/run-volume-2026-11-push-ci-coalesce.md` if not yet folded, else the folded agents.md text |
| Workflow name `Integration PR readiness check` | no run carries it any more | the gate list entry stays (OD3); the `ci.yml` step "Integration PR readiness check unit tests" is a step name for the script's tests and stays |
| File `.github/workflows/lint-plan-archival.yml`, job id `lint` (OD2) | deleted | `.claude/commands/audit-plans.md`, `.claude/commands/implement-plan-ai.md` and their copies under `workflow-templates/.claude/commands/`; `tests/test_mark_stable_release_tag_refspec_contract.py` (a comment); `tests/test_workflow_checkout_integration_ref_audit.py`; `docs/INVENTORY.md`; `agents.md`; `scripts/pr_checks_lib.sh` and `scripts/persist_orchestrator.py` comments that count the jobs named `lint` |
| Check-run name `readiness` | gone | none: no script, heal rule or log parser reads it (searched `scripts/`, `tests/`, `.github/`, `prompts/`) |
| Check-run name `lint` from the PR-body lint and from plan-archival | gone; `ci.yml`'s `lint` stays | the gate entry `lint` stays (CI's aggregate, and heads from before the fold); `tests/test_pr_checks_lib_required_filter.py` cross-workflow `lint` supersession tests stay valid, their docstrings and the `_mark_superseded` comment stop saying three workflows share the name; after the fold both failures block through `pr-body-lint` |
| Check-run name `pr-body-lint` | new (OD1) | added to `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` in `scripts/pr_checks_lib.sh` and `scripts/orchestrate_poll_process.sh`; README `ORCH_FINAL_MERGE_REQUIRED_CHECKS` default column; the default-string pins in `tests/test_pr_checks_lib_required_filter.py` and `tests/test_orchestrate_final_merge_required_checks_gate.py` |
| Status context `orchestrator/integration-pr-not-ready` | kept | unchanged |

Release manifests and the `@stable` sync: none of the three workflows is under `workflow-templates/`, in a profile manifest or in `sync-manifest.json`, so consumers never had them. The synced files that change are `CLAUDE.md` and the two command files.

### How the old names retire

- A `pull_request` run uses the workflow files of the PR's merge ref. After the fold merges, the next event on an open PR runs `PR checks` and none of the three old workflows.
- A PR that gets no further event keeps the check-runs and the status already on its head. The lints there are named `lint`, which the gate keeps reading, and the status context is unchanged, so it merges as before. A head that runs `PR checks` carries `pr-body-lint`, which the gate reads from the same PR on, so a passing `pr-body-lint` alone never stands in for an unchecked body. `lint` never leaves the list (CI owns it), so no deprecation window is needed for either name.
- A PR whose branch conflicts with `main` runs no `pull_request` workflow until the conflict is resolved, today and after.
- Promotion PRs into `stable` take the workflow from their merge ref, which contains `main`'s files, so they switch with `main`.
- Consumers pinned to an older stable read the old gate list. Consumers on the next stable read the list with `pr-body-lint` added; no consumer workflow produces that check-run, and a listed name with no check-run blocks nothing, so their merges are unchanged. No entry is removed (OD3: A).

### Manual steps, per repository

| Repository | Step | When |
| --- | --- | --- |
| coding-workflows | none required for the fold | |
| coding-workflows | none for OD5 (B recommended); OD5: A is not safe while label events skip the job | |
| each consumer | none | the ruleset `coding-workflows managed wrappers` requires the status the `pull_request_target` run posts, which Phase 2 does not touch |
| `binance-blessings` | none from this plan (OD4) | |

### Jobs per pull request event (§33.B)

coding-workflows, a PR into `main` or `stable`, counting only the workflows this plan touches:

| Event | Before | After |
| --- | --- | --- |
| `opened`, `synchronize`, `reopened`, `ready_for_review` | 2 jobs (`readiness`, the PR-body lint's `lint`), 3 when the PR changes `docs/completed/**` (plus the plan-archival `lint`) | 1 |
| `edited` | 1 (the PR-body lint's `lint`), 2 with `docs/completed/**` | 1 |
| `labeled` / `unlabeled`, override label | 1 (`readiness`) | 1 |
| `labeled` / `unlabeled`, any other label | 0 jobs, 1 skipped run | 0 jobs, 1 skipped run |

The saving is one job per event of the first row, two when the PR changes `docs/completed/**`, and one per `edited` event that changes `docs/completed/**`. The audit counted runs per workflow, not events per type, so it does not give that number directly: the 1,015 PR-body lint runs include `edited` events, which save nothing in the common case, so the first-row count is below 1,015 by the number of `edited` events. Coalescing (D5) removes more when pushes and edits arrive in bursts.

Consumers: one `AI Workflow Integrity` push run per push before; after, one only for a push that changes `.github/workflows/**`. The `pull_request_target` run is unchanged, one per `opened`, `synchronize` and `reopened`.

No job, workflow or matrix leg is added. One workflow file is added and three are removed.

## Phases & Merge Strategy

1. **Phase 1: fold the three checks (coding-workflows only).** Adds `.github/workflows/pr-checks.yml`; deletes the three old workflow files; updates the readers listed above; one changelog fragment and one agents fragment.
   - Done when: the tests in the Tests section pass; `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` lists `pr-body-lint` in both scripts; `PR checks` runs on the phase's own PR and reports `pr-body-lint`; the PR's head carries the status `orchestrator/integration-pr-not-ready`; no workflow other than `ci.yml` has a job named `lint` (holds with OD2: A; with OD2: B, `lint-plan-archival.yml` keeps its `lint` job and this criterion is dropped).
   - Rollback: revert the PR. The old files return, and open PRs pick them up on their next event.
2. **Phase 2: path-filter the consumer integrity wrapper's push trigger.** Changes `workflow-templates/ai-workflow-integrity.yml` and its tests and docs.
   - Done when: `tests/test_workflow_integrity.py` passes with the new trigger pinned, and a test proves every managed candidate path the reusable workflow lists lies under `.github/workflows/`.
   - Rollback: revert the PR; consumers get the old wrapper back on the next `@stable` sync.

The phases touch disjoint files and can merge in either order.

## Implementation Steps

### Phase 1

0. On the current `main` tip, re-verify the Context bullets: the job ids in the three old files, the `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` string in both scripts, and the reader lists in Names that change. Where the tree differs, follow the tree and correct the plan in the same PR.
1. Create `.github/workflows/pr-checks.yml` as described under Approach. Move each step body verbatim; keep each step's `name:`. Pass every `${{ }}` value through `env:` as the old files do. (Only if OD1 is answered B: name the job `pr-checks` instead.)
2. Delete `.github/workflows/integration-pr-readiness.yml`, `.github/workflows/lint-pr-body-auto-close.yml` and `.github/workflows/lint-plan-archival.yml` (the last only with OD2: A).
3. Update `tests/test_lint_pr_body_auto_close.py` and `tests/test_check_integration_pr_readiness.py` to read `pr-checks.yml`. Keep every existing assertion about step content and order. (`tests/test_lint_plan_archival_completeness.py` tests the script only and does not read the workflow; it needs no change.)
4. Update `tests/test_workflow_checkout_integration_ref_audit.py`: one registry entry for `pr-checks.yml` replaces three.
5. Correct text in place: `CLAUDE.md` §19 enforcement item 1 (new workflow name and file; the section number and the rule are unchanged; there is no `workflow-templates/CLAUDE.md` to edit), `unattended_system_instructions.md` if it names the old file, the two command files and their template copies, `docs/INVENTORY.md`, the README `ORCH_FINAL_MERGE_REQUIRED_CHECKS` prose, the README `AUTO_MERGE_REQUIRED_CI_CHECK` row (it says `lint-pr-body-auto-close.yml` and `lint-plan-archival.yml` also have a job called `lint`), and the agents.md paragraphs that name the old files. Run `make generate` for the TREE block.
6. Gate list (required, not optional): append `pr-body-lint` to `ORCH_FINAL_MERGE_REQUIRED_CHECKS_DEFAULT` in `scripts/pr_checks_lib.sh` and `scripts/orchestrate_poll_process.sh`, keeping the two strings identical and every existing entry, `lint` included. Update the README `ORCH_FINAL_MERGE_REQUIRED_CHECKS` default column and prose, and the default-string pins in `tests/test_pr_checks_lib_required_filter.py` and `tests/test_orchestrate_final_merge_required_checks_gate.py`. Comments in `scripts/pr_checks_lib.sh` and `scripts/persist_orchestrator.py` that count three jobs named `lint` now say only `ci.yml` has one. The CI-run binding (`PR_CHECKS_CI_RUN_IDS`) and the `lint` supersession rule stay, because branches older than the fold still carry the old check-runs. (Only if OD1 is answered B: use `pr-checks` in place of `pr-body-lint`.)
7. Tests: see Tests.
8. Add `changelog.d/<pr>-fold-per-pr-checks.md` (`changed`, naming the new gate-list entry) and `agents.d/<pr>-fold-per-pr-checks.md` (section "Workflow architecture").

### Phase 2

1. `workflow-templates/ai-workflow-integrity.yml`: add `paths: ['.github/workflows/**']` under `push:`. Update the header comment.
2. `tests/test_workflow_integrity.py` (and `tests/test_update_workflows_guardrails.py` if it pins the trigger): pin the new trigger; add the candidate-path test.
3. Correct the agents.md paragraph "Managed wrappers are locked in consumer repos" in place ("on every branch `push`" becomes "on a branch `push` that changes `.github/workflows/**`").
4. Add `changelog.d/<pr>-integrity-push-paths.md` (`changed`).

## Files & Modules

Phase 1:

- `.github/workflows/pr-checks.yml` [new]
- `.github/workflows/integration-pr-readiness.yml` [del]
- `.github/workflows/lint-pr-body-auto-close.yml` [del]
- `.github/workflows/lint-plan-archival.yml` [del] (OD2: A)
- `tests/test_pr_checks_workflow.py` [new]
- `tests/test_lint_pr_body_auto_close.py`, `tests/test_check_integration_pr_readiness.py`, `tests/test_workflow_checkout_integration_ref_audit.py`, `tests/test_mark_stable_release_tag_refspec_contract.py`, `tests/test_pr_base_freshness_gate.py`, `tests/test_pr_checks_lib_required_filter.py`, `tests/test_orchestrate_final_merge_required_checks_gate.py`
- `scripts/pr_checks_lib.sh`, `scripts/orchestrate_poll_process.sh` (gate default gains `pr-body-lint`; comments), `scripts/persist_orchestrator.py` (comments)
- `CLAUDE.md`, `unattended_system_instructions.md`, `.claude/commands/audit-plans.md`, `.claude/commands/implement-plan-ai.md`, `workflow-templates/.claude/commands/audit-plans.md`, `workflow-templates/.claude/commands/implement-plan-ai.md`
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/<pr>-fold-per-pr-checks.md` [new], `agents.d/<pr>-fold-per-pr-checks.md` [new]

Phase 2:

- `workflow-templates/ai-workflow-integrity.yml`
- `tests/test_workflow_integrity.py`, `tests/test_update_workflows_guardrails.py`
- `agents.md`
- `changelog.d/<pr>-integrity-push-paths.md` [new]

## Tests

Phase 1, new `tests/test_pr_checks_workflow.py`:

- The workflow has exactly one job, its id is `pr-body-lint` (OD1), and it has no job-level `name:`.
- Trigger types and branches equal the union stated under Approach; there is no `paths:` filter.
- `permissions` equal the four in D4.
- Every step after the checkout has an `if:` that starts with `!cancelled()`.
- "Check integration PR readiness and post status" has `continue-on-error: true`, and no lint step has `continue-on-error`.
- The job-level `if:` and the concurrency group agree: for each of the event shapes (non-label event, override label, other label) the group is the per-PR group exactly when the job runs. Evaluated by a small expression table in the test, not by string equality alone.
- `cancel-in-progress` is false.
- No workflow under `.github/workflows/` other than `ci.yml` has a job with id `lint` (a new guard; no test pins this today).
- The plan-archival diff: in a temporary repository with a base commit, a head commit that adds `docs/completed/x.md` and a merge commit, the enumerate step's command prints that path; a pure move from `docs/plans/` into `docs/completed/` is not printed (rename detection kept); with a non-merge `HEAD` it exits non-zero.

Phase 1, existing tests updated: the two per-check test files that read a workflow keep their assertions against the new file; the default-string pins in `tests/test_pr_checks_lib_required_filter.py` and `tests/test_orchestrate_final_merge_required_checks_gate.py` include `pr-body-lint`, and the drift test keeps the two scripts equal. `tests/test_pr_checks_lib_required_filter.py` gains: a failed `pr-body-lint` blocks under the default set; a failed `pr-body-lint` still blocks when a newer `pr-body-lint` with conclusion `skipped` exists on the same head (the label-event skip); a failed `lint` still blocks (CI and pre-fold heads).

Phase 1, on the PR itself: `PR checks` runs once per push, reports `pr-body-lint`, and posts the readiness status; the three old workflows do not run.

Phase 2: the trigger pin and the candidate-path test above.

Both phases: `bash -n`, shellcheck, `yamllint -s`, `make generate-check`, `python3 scripts/assemble_agents.py check --repo-root .`, `python3 tests/inventory_parity.py`, and the affected-test selection of `.claude/hooks/prepush_affected_tests.py`.

## Risks & Mitigations

- **A failing step hides the others' results.** Mitigated by D2 and its test.
- **A readiness script failure fails the gated job.** The script exits 1 when it cannot post the status or cannot fetch the tracking issue; without care that would fail `pr-body-lint`, which the gate now lists and never supersedes once failed. Mitigated by `continue-on-error: true` on the readiness step (D2), so such a failure blocks exactly what it blocks today (nothing in the gate), and by its test.
- **A skipped label run displaces a waiting real run.** Mitigated by D5 and its test.
- **`fetch-depth: 2` and a merge ref GitHub has not refreshed.** The plan-archival lint then judges a slightly older base. It reads only the files the PR adds, so an older base cannot hide an added file. ACCEPTED.
- **The folded job gets a name the gate does not list.** A failed PR-body or plan-archival lint would then stop blocking. Mitigated by step 6, which adds `pr-body-lint` to the default in the same PR, and by its tests.
- **A skipped `pr-body-lint` from a label event.** The pipeline's gate accepts `skipped` and still blocks on an earlier failed run of the name; GitHub's ruleset would treat the skip as passing, so OD5 recommends not requiring the name there. Mitigated by OD5: B and the skipped-after-failed test.
- **A stale failed `lint` on a head from before the fold.** The gate evaluates every failed required-name check-run on its own, so such a head stays blocked until its next push, exactly as today. ACCEPTED.
- **`CLAUDE.md` text changes reach consumers.** Only the workflow name and file in §19's enforcement note change; consumers never had the workflow. ACCEPTED.
- **Phase 2 hides a wrapper change made outside `.github/workflows/`.** There is none by construction; the candidate-path test proves it and fails if the reusable workflow ever lists a candidate elsewhere.
- **A direct push that changes only the integrity wrapper's pin.** The file is under `.github/workflows/`, so the push run still starts.
- **`binance-blessings`' 447 `Updater Gate Check` runs are untouched.** ACCEPTED — pending OD4.

## Rollout

Phase 1 is effective on merge, for each open PR at its next event. Phase 2 reaches consumers on the next `@stable` sync (`.github/workflows/update_workflows.yml`, daily 04:00 UTC plus the release dispatch); the sync PR updates the wrapper and its pin together, and the integrity run that judges that PR is the base branch's old wrapper. Rollback of either phase is a revert.

## References

- `docs/plans/reduce-actions-run-volume-plan.md` (item 10 filtered the readiness check's label events)
- `docs/plans/cut-ci-runner-time-plan.md` (final CI at the end of review; `CI quick`)
- `docs/postmortems/2026-05-18-project-2734-stall.md` (P1, P3, P6: why the three checks exist)
- PR #9570 (the PR-body `lint` passed about six hours before CI's `lint`)
- CLAUDE.md §6, §19, §27, §33
