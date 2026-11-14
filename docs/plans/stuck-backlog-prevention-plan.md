# Stop runner-image drift, red-main cascades and queue-driven re-dispatch from jamming the pipeline

## Summary

Three independent harness changes that stop the failure loop seen on 2026-11-11, when 91 issues and 63 PRs were open at once. Phase 1 pins every hosted job to an explicit runner image and adds a nightly canary that runs the full CI on `ubuntu-latest`, so the next image move shows up as one heal issue instead of red CI everywhere. Phase 2 holds plan auto-approval while the default branch's CI is red and tracked by a heal issue, so new implementation PRs do not pile onto a broken base. Phase 3 makes the poller notice deep Actions queues: it holds its own dispatches until the queue drains and links queued pipeline runs to their issue, so it stops re-triggering work that is only waiting for a runner.

## Context

On 2026-11-11 the backlog reached 91 open issues and 63 open PRs. The causes, measured that day:

- **Runner image drift.** 42 workflow files (97 `runs-on: ubuntu-latest` lines) and 3 files under `workflow-templates/` follow `ubuntu-latest`. When it moved to `ubuntu-26.04` (Python 3.14, Rust coreutils), `scripts/render_prompt.py`, the millisecond timers and `tests/test_gh_retry_stdout_isolation.py::test_closed_reader_does_not_rerun_a_successful_command` broke. `CI` on `main` has been red since `aa8aa93` (2026-11-11 00:40 UTC), tracked by heal issue #9215. The fixes are in PRs #9240 (merged), #9345 and #9246.
- **Cascade from a red base.** Every PR inherited the red base. Check-failure triage filed 42 `CI failure: CI on PR #N` issues, most concluding "not caused by this PR". Each went clarify → plan → auto-approve → implement and opened an `AI implementation for issue #N` PR (about 25), which inherited the same red base and filed the next triage generation (the cap is 3). PR #9272 stops triage from filing when the base failure is already tracked. Nothing stops the plan → implement half: `plan.yml` posts `/approved [auto-approved-by-plan]` whenever `AUTO_IMPLEMENT_ON_CLEAR_PLAN` is on (`.github/workflows/plan.yml:3203-3548`), whatever the base's state.
- **Runner saturation.** About 2,500 runs were created that day; 493 were queued at once against about 20 concurrent hosted runners. Required checks could not start, so PRs sat `blocked`.
- **Stall recovery re-dispatching queued work.** `build_active_issue_set` (`scripts/orchestrate_poll_process.sh:18238`) maps runs to issues only through `head_branch` patterns (`ai/issue-N`). A plan run triggered by an `/answer` comment runs on the default branch, so it never maps to its issue. The function also reads at most 50 queued runs and treats any run older than `STALL_THRESHOLD_MINUTES` (default 120) as a zombie. With hundreds of runs queued, issues #9004, #9008, #9014 and #9056 each got 5-6 `retrigger_plan` comments that day, about every 1-2 hours, each adding another queued run.
- **Existing mechanisms to extend (§5).** `phase_cap_can_dispatch` (`scripts/orchestrate_poll_process.sh:17824`) already gates every poller dispatch (stall recovery at 19757, 25539, 26738; wave issue creation at 29846, 34501; validation, review-blocked judge). `.github/ai/concurrency_caps.yml` has `global_max_concurrent: -1`. `_plan_auto_approval_gate` (`:19414`) already gates both poller `/approved` paths (`auto_approve`, and `retrigger_implement` from `ai:awaiting-approval`). `_load_actions_runs_cached` (`:17620`) already reads `actions/runs?status=queued&per_page=50`, whose response carries `total_count`, but the `--jq '.workflow_runs'` filter drops it. Run names already carry `[pr:N]` (`internal-review.yml`) and `[tracking:N]` (`internal-validate.yml`). `workflow-failure-heal-intake.yml` already watches `Nightly Validation Self-Test`, so a failing nightly job files a heal issue automatically.

## Goals

- No hosted job follows a moving image label. A new image is tried nightly and, if it breaks CI, produces one heal issue while PR CI stays green.
- While the default branch's CI is red and tracked, plans that finish are held instead of auto-approved, and approved once the base is green again. The heal fix itself is never held.
- When 150 or more runs are queued, the poller makes no new dispatches and does not count stall time toward recovery, until fewer than 50 are queued.
- A queued or running clarify, plan, implement or clarify-respond run is recognised as the issue's active run, whether or not it runs on an `ai/issue-N` branch.

## Non-goals

- Closing a closed project's leftover issues and PRs in general (user decision Q15: A). Smoke-test leftovers are covered by #8953 / PR #8960.
- Triage evidence capture (the log tail ending before the failed step). PRs #9218 and #9219 cover it; the `reviewers_failed` failure blocking them is handled separately.
- Suppressing triage issues for a tracked red base. That is PR #9272.
- A required per-PR canary job (user decision Q12: A).
- Paginating the queued-runs read beyond 50 rows.

## Constraints

- **§6 naming immutability.** No identifier is renamed or removed. The workflow `name:` of every touched workflow stays the same. Adding `run-name:` changes only `display_title`; Phase 3 audits every `display_title` reader in `scripts/` (31 matches) before it changes anything. New names, checked for collisions on 2026-11-12 (0 matches each): `RUNNER_IMAGE` input, `RED_BASE_APPROVAL_HOLD_ENABLED`, `RED_BASE_HOLD_WORKFLOWS`, `RED_BASE_APPROVAL_HOLD` log prefix, `ai:red-base-approval-hold:v1` marker, `ACTIONS_QUEUE_BACKPRESSURE_HIGH`, `ACTIONS_QUEUE_BACKPRESSURE_LOW`, `QUEUED_RUN_ZOMBIE_MINUTES`, `QUEUE_BACKPRESSURE` log prefix. Re-check each before use.
- **§4 env vars.** Every new variable has a default: `RED_BASE_APPROVAL_HOLD_ENABLED=true`, `RED_BASE_HOLD_WORKFLOWS=.github/workflows/ci.yml`, `ACTIONS_QUEUE_BACKPRESSURE_HIGH=150` (`-1` disables), `ACTIONS_QUEUE_BACKPRESSURE_LOW=50`, `QUEUED_RUN_ZOMBIE_MINUTES=1440`.
- **§15 GitHub API hygiene.** Phase 1 adds no calls. Phase 2 adds, only when an auto-approval or a poller approval is about to happen, one open-issues read (`issues?labels=ai:workflow-heal&state=open&per_page=100`) and at most one runs read per configured workflow (`actions/workflows/<file>/runs?branch=<default>&event=push&status=completed&per_page=1`). The poller caches both per tick, so N approvals in a tick cost the same two reads. Phase 3 adds zero calls: it keeps `total_count` from the queued read the poller already makes. Every new read fails open (no hold, legacy behaviour).
- **§27 workflow file size.** `plan.yml` is 228,098 bytes, `ci.yml` 189,348, `orchestrate_poll.yml` 140,583 and `nightly-validation-selftest.yml` 22,755. The guard is 480,000. New logic goes into `scripts/`; workflows gain only `run-name:`, `runs-on:`, inputs, `env:` lines and short calls. Check with `wc -c` after editing.
- **§14 consumers.** Phases 1 and 3 change files under `workflow-templates/` and reusable workflows, which reach all 13 repositories in `.github/ai/consumer_repos.json` on the next `@stable` sync. Phase 2's hold uses `RED_BASE_HOLD_WORKFLOWS`, so a consumer whose CI file is not `.github/workflows/ci.yml` gets no hold (legacy behaviour) until it sets the variable.
- **Security (§1).** The hold trusts a heal issue only when its author is the pipeline login or an OWNER/MEMBER/COLLABORATOR, matched by the existing `<!-- workflow-failure-heal:workflow=<path> -->` marker and the `- **Target branch:** \`<branch>\`` line. Text from untrusted authors never creates a hold.
- **§19.** PR bodies use `Refs #N`; no auto-close keywords against `ai:orchestrator-tracking` issues.
- **§20 / §30.** Each phase ships one `changelog.d/` fragment and one `agents.d/` fragment.
- **§18.** No standalone script, supervisor or DB operation.

## Automation (§18.E)

- **New script, extended script, or existing code only:** extends existing code only. Phase 2 adds one subcommand to `scripts/orchestrate_lib.py` and one helper script body under `scripts/` that the existing `plan.yml` step sources; no standalone manually-run script.
- **Scheduler / entry point:** Phase 1's canary runs inside the existing `.github/workflows/nightly-validation-selftest.yml` (cron `15 2 * * *`), calling `.github/workflows/ci.yml` through a new `workflow_call` trigger. Phase 2 runs in the existing auto-approval step of `.github/workflows/plan.yml` and in `_plan_auto_approval_gate` of `scripts/orchestrate_poll_process.sh` (run by `.github/workflows/orchestrate_poll.yml`). Phase 3 runs in `scripts/orchestrate_poll_process.sh` and `.github/workflows/orchestrate_poll.yml`.
- **Long-running supervisor:** none.
- **DB work:** none.
- **`docs/scripts-pending-removal.md` entry:** none.

## Approach

### Phase 1: pin the image, canary the next one

Replace every `runs-on: ubuntu-latest` in `.github/workflows/*.yml` and `workflow-templates/**` with `ubuntu-26.04` (user decisions Q7: A, Q10: A, Q11: A). This is the image those jobs already run on, so no behaviour changes on merge. In `ci.yml`, add a `workflow_call` trigger with one optional string input, `runner_image`, and set each job's `runs-on` to `${{ inputs.runner_image || 'ubuntu-26.04' }}`. On `push` and `pull_request` events `inputs` is empty, so the expression resolves to the pin. Add a `runner-image-canary` job to `nightly-validation-selftest.yml` that calls `./.github/workflows/ci.yml` with `runner_image: ubuntu-latest`. When `ubuntu-latest` moves to an image that breaks CI, that nightly workflow fails, and `workflow-failure-heal-intake.yml`, which already watches it, files one heal issue, while PR CI stays on the pin. A contract test forbids `ubuntu-latest` anywhere except that one canary input.

### Phase 2: hold auto-approval while the base is red

Add `base-ci-health` to `scripts/orchestrate_lib.py`. It is a pure function over pre-fetched JSON: open `ai:workflow-heal` issues plus, for each workflow path in `RED_BASE_HOLD_WORKFLOWS`, its newest completed push run on the default branch. It returns `{"red": bool, "heal_issue": N, "workflow": path, "run_id": id}`. The base is red when a configured workflow's newest run concluded `failure` and a trusted open heal issue names that workflow and the default branch. Callers fetch the two reads and pipe them in.

In `plan.yml`'s auto-approval step, before posting `/approved [auto-approved-by-plan]`: unless the issue carries `ai:workflow-heal`, evaluate `base-ci-health`. When it is red, post one comment with the `<!-- ai:red-base-approval-hold:v1 heal=<N> -->` marker instead, saying the plan is ready, approval is held while `<branch>` CI is red (tracked in #N), and the poller approves it when the base is green. Log `RED_BASE_APPROVAL_HOLD outcome=held issue=<n> heal=<N> workflow=<path>`. The issue stays in `ai:awaiting-approval`. In `_plan_auto_approval_gate`, run the same check first, using the per-tick cache. Red → log `RED_BASE_APPROVAL_HOLD outcome=held` and return 1 (hold), which already does not increment the recovery count. Green → continue to the existing checks, so held issues are approved by the existing `auto_approve` stall action once the base is green. Kill switch: `RED_BASE_APPROVAL_HOLD_ENABLED`. Implement runs already started are not touched.

### Phase 3: queue backpressure and run-to-issue linkage

**Queue depth.** In `_load_actions_runs_cached`, keep `total_count` from the existing `status=queued&per_page=50` read and export it as `ACTIONS_QUEUED_TOTAL`, with empty meaning unknown. Hysteresis: backpressure turns on when `ACTIONS_QUEUED_TOTAL >= ACTIONS_QUEUE_BACKPRESSURE_HIGH` and off when it is `< ACTIONS_QUEUE_BACKPRESSURE_LOW`; in between, the previous tick's state holds. The previous state is a one-line marker file restored and saved through `actions/cache` in `orchestrate_poll.yml`, the same pattern as the existing day markers (`orchestrate_poll.yml:1307-1883`). A missing marker means off. An unknown count leaves the state unchanged.

**Dispatch hold.** At the top of `phase_cap_can_dispatch`, before the `PHASE_CAPS_ENABLED` early return, when backpressure is on and the action is not `attempt_merge`, `escalate_human` or `skip`: log `QUEUE_BACKPRESSURE outcome=held state=<s> action=<a> issue=<n> queued=<total>` and return 1. Every existing call site already treats a 1 as "take no action, do not increment recovery". So stall-recovery retriggers, wave issue creation, validation dispatch and the review-blocked judge pause, and no stall time is counted toward recovery while the queue is deep. Event-driven runs (a human comment, a PR push) are not affected. Log one `QUEUE_BACKPRESSURE outcome=on|off queued=<total> high=<h> low=<l>` line per tick when the state changes.

**Run linkage.** Add `run-name: "<existing workflow name> [issue:${{ github.event.issue.number }}]"` to `internal-clarify.yml`, `internal-plan.yml`, `internal-implement.yml`, `internal-orchestrate-clarify-respond.yml` and their `workflow-templates/` counterparts (`ai-plan.yml`, `ai-implement.yml` and the clarify / clarify-respond wrappers). In `build_active_issue_set`, also extract `[issue:N]` from `display_title` of fresh runs. Queued runs use `QUEUED_RUN_ZOMBIE_MINUTES` (default 1440) instead of the general stall cutoff, so a run waiting hours for a runner is still active, while stuck GitHub-side ghost rows (some dated 2026-08-19) are still excluded.

## Phases & Merge Strategy

Each phase is its own PR, independently mergeable, complete on its own and production-safe at merge. No phase assumes another has merged.

### Phase 1: Pin the runner image and add the nightly canary
Safe at merge: the pin names the image the jobs already use. The canary only adds a nightly job. Rollback: revert the PR.

### Phase 2: Hold plan auto-approval while the tracked base is red
Safe at merge: on by default, but a no-op whenever the base is green or no trusted heal issue matches. Every read fails open. Rollback: `RED_BASE_APPROVAL_HOLD_ENABLED=false`.

### Phase 3: Queue backpressure and `[issue:N]` run linkage
Safe at merge: backpressure is on by default at 150/50. `ACTIONS_QUEUE_BACKPRESSURE_HIGH=-1` turns it off. Run-name and linkage only add matches. Rollback: the variable, or revert the PR.

## Implementation Steps

### Phase 1
1. Replace `runs-on: ubuntu-latest` with `runs-on: ubuntu-26.04` in every `.github/workflows/*.yml` and `workflow-templates/**` file (97 + 3-file occurrences as of 2026-11-12; recount before editing). Leave `ubuntu-24.04` jobs (`review_autofix_sweep.yml`, `drift-audit.yml`, `security-audit.yml`) unchanged.
2. `ci.yml`: add `workflow_call:` with input `runner_image` (type `string`, required `false`, default `""`). Set each of the 8 jobs' `runs-on` to `${{ inputs.runner_image || 'ubuntu-26.04' }}`. Audit every step guarded on `github.event_name == 'push'` or `'pull_request'` and confirm it is skipped under `workflow_call`. Under `workflow_call`, `github.event_name` is the caller's event (`schedule` or `workflow_dispatch`), so no push-only side effect (heal intake, budget watch) may run from the canary. Confirm the `concurrency` group cannot cancel a push run.
3. `nightly-validation-selftest.yml`: add job `runner-image-canary`: `uses: ./.github/workflows/ci.yml`, `with: { runner_image: ubuntu-latest }`, `permissions: contents: read`, `secrets: inherit` only if `ci.yml` needs a secret (verify; prefer none).
4. If actionlint 1.7.12 (`ci.yml:299`) rejects `ubuntu-26.04` as an unknown label, add `.github/actionlint.yaml` with `self-hosted-runner: labels: [ubuntu-26.04]`, and remove it once actionlint knows the label.
5. Add `tests/test_runner_image_pin.py`: it fails if any `.github/workflows/*.yml` or `workflow-templates/**` contains `ubuntu-latest` outside `nightly-validation-selftest.yml`'s canary `with:` block. It also checks that `ci.yml`'s jobs use the `inputs.runner_image || '<pin>'` form with the same pin as the other workflows. Wire it into `ci.yml` (`tests/test_ci_wires_every_test_file.py`).
6. Update fixtures only if a test asserts `ubuntu-latest` in a real workflow, not in a synthetic fixture (`tests/test_update_workflows_guardrails.py:319` and `tests/test_release_static_preflight_contract.py:84` are synthetic; leave them).
7. `changelog.d/<issue>-runner-image-pin-canary.md` (`changed`) and `agents.d/<issue>-runner-image-pin-canary.md`: document the pin, how to move it (change the pin after the canary is green on the new image), and the canary heal path.

### Phase 2
1. `scripts/orchestrate_lib.py`: add subcommand `base-ci-health`. Args: `--default-branch`, `--workflows` (comma list), `--trusted-login`, and stdin JSON `{"heal_issues": [...], "runs": {"<path>": [run]}}`. Trust: author is `--trusted-login` or `author_association` in OWNER/MEMBER/COLLABORATOR. Match: `<!-- workflow-failure-heal:workflow=<path> -->` and the `Target branch` line equal to the default branch. Output one JSON line. Errors print `{"red": false, "error": "<reason>"}` and exit 0 (fail open).
2. Add `scripts/red_base_approval_hold.sh` with a function `red_base_approval_hold_check <issue> <labels_json>`. It performs the two reads with `gh_retry` and `_safe_gh_jq`, calls `base-ci-health`, and sets `RED_BASE_HOLD_RED`, `RED_BASE_HOLD_HEAL` and `RED_BASE_HOLD_WORKFLOW`. It honours `RED_BASE_APPROVAL_HOLD_ENABLED` and skips issues labelled `ai:workflow-heal`. It is a sourced library, not a manually-run script.
3. `plan.yml` auto-approval step (`:3203-3548`): add `env:` lines `RED_BASE_APPROVAL_HOLD_ENABLED: ${{ vars.RED_BASE_APPROVAL_HOLD_ENABLED || 'true' }}` and `RED_BASE_HOLD_WORKFLOWS: ${{ vars.RED_BASE_HOLD_WORKFLOWS || '.github/workflows/ci.yml' }}`. Source the helper and call it immediately before the `/approved [auto-approved-by-plan]` post. When red, post the hold comment (marker `<!-- ai:red-base-approval-hold:v1 heal=<N> -->`, once per issue: skip if the newest pipeline-login comment already carries that marker for the same heal issue) instead of the approval, and log `RED_BASE_APPROVAL_HOLD`. Tag the Telegram notice `INFO`, not CRITICAL.
4. `scripts/orchestrate_poll_process.sh`: source the helper. In `_plan_auto_approval_gate`, run the check first, caching the two reads per tick (§15 cycle-local cache) in a new `RED_BASE_HEALTH_CACHE` variable (check the name for collisions). Red → log and return 1. Pass `RED_BASE_APPROVAL_HOLD_ENABLED` and `RED_BASE_HOLD_WORKFLOWS` into the poller's `env:` in `orchestrate_poll.yml` with the same defaults.
5. Add `tests/test_red_base_approval_hold.py` covering `base-ci-health` (all match rules, trust, fail-open), the `plan.yml` step body against stubs (hold, green, heal-labelled exempt, kill switch, read failure) and the poller gate. Wire it into `ci.yml`.
6. `changelog.d/<issue>-red-base-approval-hold.md` (`added`) and `agents.d/<issue>-red-base-approval-hold.md`: document the variables, the marker, the log prefix under `## Stable log prefixes (contractual)`, and that a consumer with another CI file sets `RED_BASE_HOLD_WORKFLOWS`.

### Phase 3
1. `_load_actions_runs_cached`: change the queued read to keep `total_count` (one jq expression returning `{total_count, workflow_runs}`) and export `ACTIONS_QUEUED_TOTAL`. No new API call.
2. `orchestrate_poll.yml`: restore and save marker `queue-backpressure-state` through `actions/cache`, keyed like the existing day markers. Pass its path as `QUEUE_BACKPRESSURE_STATE_FILE`, plus `ACTIONS_QUEUE_BACKPRESSURE_HIGH: ${{ vars.ACTIONS_QUEUE_BACKPRESSURE_HIGH || '150' }}`, `ACTIONS_QUEUE_BACKPRESSURE_LOW: ${{ vars.ACTIONS_QUEUE_BACKPRESSURE_LOW || '50' }}` and `QUEUED_RUN_ZOMBIE_MINUTES: ${{ vars.QUEUED_RUN_ZOMBIE_MINUTES || '1440' }}`.
3. `orchestrate_poll_process.sh`: once per tick, after the runs load, compute `QUEUE_BACKPRESSURE_ACTIVE` from the count, the thresholds and the restored state. Write the new state to the marker file and log `QUEUE_BACKPRESSURE outcome=on|off` on change. A non-numeric threshold or `HIGH=-1` disables it.
4. `phase_cap_can_dispatch`: insert the backpressure check before the `PHASE_CAPS_ENABLED` early return, exempting `attempt_merge`, `escalate_human` and `skip`. Confirm at each call site that a 1 means no action and no recovery increment (19605, 19757, 20757, 20886, 25539, 26738, 27419, 29846, 34501, 16203).
5. Audit the 31 `display_title` readers in `scripts/` (`grep -n display_title scripts/*.sh scripts/*.py`). For each, record whether it reads runs of the four workflows and whether adding ` [issue:N]` changes its match. Adjust only readers whose match would break, keeping their old behaviour (§6). List the audit in the PR body.
6. Add `run-name:` to the four `internal-*.yml` wrappers and their `workflow-templates/` counterparts. `run-name` is evaluated once per run for the triggering event; `github.event.issue.number` exists on both `issues` and `issue_comment`.
7. `build_active_issue_set`: add `[issue:N]` extraction from `.display_title`. Apply `QUEUED_RUN_ZOMBIE_MINUTES` to runs with `status == "queued"`, and the existing per-run threshold to the rest.
8. Tests: extend `tests/test_orchestrate_poll_process.py` (or add `tests/test_queue_backpressure.py`, wired into `ci.yml`) for hysteresis (on at HIGH, held between, off below LOW, missing marker, unknown count), the dispatch hold and its exemptions, `[issue:N]` linkage, and the queued zombie cutoff. Add a contract test that the four wrappers and their templates carry the `[issue:N]` run-name.
9. `changelog.d/<issue>-queue-backpressure.md` (`added`) and `agents.d/<issue>-queue-backpressure.md`: document the variables, the log prefix, the run-name convention and the exemptions.

## Files & Modules

| Phase | Files |
| --- | --- |
| 1 | `.github/workflows/*.yml` (42 files), `workflow-templates/**` (3 files), `.github/workflows/ci.yml`, `.github/workflows/nightly-validation-selftest.yml`, optional `.github/actionlint.yaml`, `tests/test_runner_image_pin.py`, fragments |
| 2 | `scripts/orchestrate_lib.py`, `scripts/red_base_approval_hold.sh`, `.github/workflows/plan.yml`, `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml`, `tests/test_red_base_approval_hold.py`, `.github/workflows/ci.yml` (wiring), fragments |
| 3 | `scripts/orchestrate_poll_process.sh`, `.github/workflows/orchestrate_poll.yml`, `.github/workflows/internal-clarify.yml`, `internal-plan.yml`, `internal-implement.yml`, `internal-orchestrate-clarify-respond.yml`, matching `workflow-templates/` wrappers, tests, `.github/workflows/ci.yml` (wiring), fragments |

## Data Model / Index Changes

None. No MongoDB collection, index or contract is touched (§10).

## Tests

- Phase 1: `tests/test_runner_image_pin.py`; `actionlint` and `yamllint` on every changed workflow; `make generate-check`. After merge, one manual `workflow_dispatch` of `Nightly Validation Self-Test` to see the canary call complete.
- Phase 2: `tests/test_red_base_approval_hold.py`, as listed in step 5. Each new test fails without the change.
- Phase 3: the hysteresis, dispatch-hold, linkage and run-name contract tests in step 8, plus the existing `tests/test_orchestrate_poll_process.py` and `tests/test_stall_recovery_pr_lookup.py` suites.
- Every phase: the pre-push test gate, with 0 failures caused by the change.

## Risks & Mitigations

| Risk | Mitigation |
| --- | --- |
| A jammed backlog repeats while these phases are in flight. | Hand-off to the orchestrator happens only after `main` is green and the queue has drained (user decision Q6: A). |
| actionlint 1.7.12 does not know `ubuntu-26.04`. | Phase 1 step 4: a one-label `.github/actionlint.yaml`. |
| Calling `ci.yml` from the nightly workflow runs push-only side effects. | Phase 1 step 2 audits every `github.event_name` guard; a contract test asserts no push-only step runs under `workflow_call`. |
| The canary doubles daily CI load. | One full CI run a day (8 jobs) against about 2,500 runs a day; it runs at 02:15 UTC. |
| The hold freezes work the heal fix itself needs. | `ai:workflow-heal` issues are exempt; only auto-approval is held, and a human `/approved` still works. |
| A stale or forged heal issue holds approvals forever. | Hold needs both an open trusted heal issue and a failing newest run of that workflow; a green run releases it on the next approval attempt. |
| Backpressure delays merges. | `attempt_merge` and `escalate_human` are exempt, and event-driven review and CI runs are unaffected. |
| The `run-name` change breaks a `display_title` reader. | Phase 3 step 5 audit before the change, with the audit listed in the PR body. |
| The cache marker is lost (eviction). | A missing marker means off; the next tick at or above HIGH turns it on again. |

## Rollout

1. The plan PR merges. Hand-off (`/implement-plan-claude`) waits until `CI` on `main` is green (PR #9345) and the queued-run count is below 50 (user decision Q6: A).
2. The orchestrator ships the three phases as three PRs, in any order.
3. After Phase 1, dispatch `Nightly Validation Self-Test` once and confirm the canary call ran CI on `ubuntu-latest`.
4. After Phase 2, the next red base produces `RED_BASE_APPROVAL_HOLD outcome=held` lines instead of new implementation PRs.
5. After Phase 3, the next deep queue produces `QUEUE_BACKPRESSURE outcome=on` and no `retrigger_plan` comments on issues whose plan run is queued.
6. Rollback per phase: see Phases & Merge Strategy.

## References

- Heal issue #9215 (red `main`), PRs #9240, #9345, #9246 (ubuntu-26.04 fixes), PR #9272 (triage skip for a tracked base failure), PRs #9218 / #9219 (triage evidence), issue #8953 / PR #8960 (smoke leftovers).
- Re-dispatched issues: #9004, #9008, #9014, #9056.
- `scripts/orchestrate_poll_process.sh`: `build_active_issue_set` (18238), `phase_cap_can_dispatch` (17824), `_plan_auto_approval_gate` (19414), `_load_actions_runs_cached` (17620), standalone `retrigger_plan` (25626).
- `.github/workflows/plan.yml:3203-3548` (auto-approval), `.github/ai/concurrency_caps.yml`, `.github/workflows/workflow-failure-heal-intake.yml:50`.
- User decisions in the interactive session of 2026-11-11/12: Q6 A, Q7 A, Q8 A, Q9 A, Q10 A, Q11 A, Q12 A, Q13 A, Q14 A, Q15 A.
