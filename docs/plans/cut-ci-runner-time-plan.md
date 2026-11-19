# Cut CI runner time: shallow checkouts, cancel replaced runs, no empty stall commits, and one full CI run per PR at the end of review

## Summary

The account's 40 concurrent GitHub-hosted jobs have been fully used for at least 12 hours, and this repo's `CI` workflow (`.github/workflows/ci.yml`) takes 90% of that runner time. This plan cuts CI's runner time in four independently mergeable phases: shallow checkouts in the slow CI jobs, cancelling PR CI that a newer push replaced, re-triggering stalled reviews without empty commits, and running the full CI suite once per PR when AI review has finished instead of on every push.

## Context

Measured on 2026-11-15 across this repo and its 13 consumers (jobs that started 17:00–05:00 UTC):

| The numbers that matter | Value |
| --- | --- |
| Concurrent jobs running, every hour of the window | 39.8 average, against an account limit of 40 |
| Runner minutes, all workflows | 28,690 |
| Runner minutes, `CI` in coding-workflows | 25,746 (89.7%), 91 runs, about 283 runner-minutes each |
| `tests-system-python` (24 matrix shards) | 1,468 jobs, 17,678 min (69% of CI) |
| Median `Checkout repository` time, jobs with `fetch-depth: 0` | 2.7 min (`tests-system-python`), 3.5 min (`tests-release-and-log-analysis`), 3.6 min (`tests-hooks-and-orchestrator`) |
| Median `Checkout repository` time, shallow jobs | 0.0–0.1 min |
| CI runs repeating a commit another CI run already covered | 18 runs, 4,820 min (19% of CI); all are the `push` + `pull_request` pair on `orchestrator/project-*` heads |
| CI runs replaced by a newer push before their first job started | 16 runs, 424 min (2% of CI) |

PR CI from 2026-11-10 to 2026-11-15: 605 `pull_request` runs on 144 non-orchestrator branches, a median of 3 per PR, 9 at the 90th percentile and 15 at most. By the head commit that triggered them:

| Head commit | Runs | Share |
| --- | --- | --- |
| `[ai-autofix] apply PR fixes` | 222 | 37% |
| `AI implementation for issue N` | 97 | 16% |
| `[standalone] stall recovery: re-trigger review for issue #N` (empty commits, 0 files) | 74 | 12% |
| `Merge branch 'main' into …` | 67 | 11% |
| `[ai-merge-resolve] resolve merge conflicts` | 60 | 10% |
| Other | 85 | 14% |

How the pieces fit today:

- `ci.yml` runs on `pull_request` for every base and on `push` to `main`, `stable` and `orchestrator/project-*`. It has no `concurrency` block. The required check is the aggregating `lint` job (the `main` ruleset requires `lint` and `ai-review/head-gate`; `_pr_wait_for_required_checks` in `scripts/pr_checks_lib.sh` requires `AUTO_MERGE_REQUIRED_CI_CHECK`, default `lint`, before auto-merge, #6963).
- Check-failure triage compares a failing PR against the base branch's newest **push** run (`scripts/check_failure_triage.sh` ~line 1380, `scripts/check_triage_resolution.py` ~line 479), so push CI on `orchestrator/project-*` must stay. `.github/workflows/internal-check-failure-triage.yml` triggers on `workflow_run` of `["CI"]`.
- `review_autofix.yml` waits up to `CHECK_RUNS_WAIT_TIMEOUT_SECS` (default 300) for check-runs on the reviewed head and feeds failures to reviewers and the editor (`collect_pr_check_runs_context.py`). A full CI run takes 12 minutes or more even with free runners, so intermediate CI results mostly arrive after the review that could use them.
- Stall recovery in `scripts/orchestrate_poll_process.sh` pushes an empty commit to re-trigger review (orchestrator arm ~line 20630, standalone arm ~line 26190). It already dispatches the review workflow directly through `_dispatch_review_for_conflicts` (~line 27526) when the last review run failed or was cancelled (`STALL_RECOVERY_EFFECTIVE_ACTION=redispatch_review_autofix`), and falls back to the empty commit otherwise.
- A GitHub Actions job that is skipped by `if:` reports success and satisfies a required check of the same name. So any design that leaves a skipped `lint` check-run on a PR head would let an untested head merge. This is why Phase 4 moves the per-push quick checks into a separate caller workflow instead of skipping jobs in `ci.yml`.

User decisions this plan implements: Q41 A+B+C, Q43 A, Q44 B, Q45 A.

## Goals

- Phase 1: the `Checkout repository` step in `tests-system-python`, `tests-hooks-and-orchestrator` and `tests-release-and-log-analysis` takes a median of 0.5 min or less, with every test still passing.
- Phase 2: a new push to a PR cancels that PR's older queued or running CI run; push CI on `main`, `stable` and `orchestrator/project-*` is never cancelled.
- Phase 3: stall recovery re-triggers review by dispatching the review workflow; it pushes an empty commit only when the dispatch itself fails.
- Phase 4: on every PR, each push runs only `static-checks` (through a new caller workflow); the full `ci.yml` suite runs once on the head that AI review finished with, and never twice for the same commit.
- Phase 4: no PR head can satisfy the required `lint` check without the full suite having run on that head.

## Non-goals

- Changing the `main` ruleset, the required check names (`lint`, `ai-review/head-gate`) or any other repository setting (§23.C).
- Consumer repos' own CI workflows. They are not managed here and use under 1% of the pool.
- Changing the content of the test suites, shard counts or timeouts beyond what Phase 1 needs.
- Ghost runs, runner capacity (plan upgrade, larger or self-hosted runners), and the poller and closed-PR sweep crons (measured at 0.6% of runner time; issue #9517 was closed as not planned).

## Constraints

- §6: no workflow, job, step, input, env var, log key or check name is renamed or removed. `ci.yml` keeps its `name: CI`, its job IDs and the `lint` aggregator. New identifiers are listed per phase and must not clash with existing ones.
- §4: every new env var or workflow input has a default; the defaults keep consumers' behaviour unchanged.
- §9: YAML 2-space indentation; shell and Python use the repo's existing style.
- §14: `review_autofix.yml`, `review_autofix_sweep.yml` and `scripts/pr_checks_lib.sh` reach consumers on the `@stable` sync. The Phase 4 hooks there are inert unless the caller passes the new input, and only `internal-review.yml` (this repo) passes it.
- §15: Phase 4 adds at most one REST read per terminal review outcome (`GET actions/workflows/ci.yml/runs?head_sha=<sha>`) plus one dispatch when needed; the sweep safety net uses one listing per sweep run, matched locally. Each new call site carries the audit comment §15 asks for.
- §19: references to tracking issues use `Refs` only.
- §20 / §30: one `changelog.d/` fragment per phase; documentation additions go through `agents.d/` fragments; existing README / agents.md text that becomes wrong is corrected in place (§30.B).
- §27: `ci.yml` is 191,686 bytes and `review_autofix.yml` must stay under the 480,000-byte guard; check with `wc -c` after each phase.

## Approach

**Phase 1, shallow checkouts.** The three jobs fetch full history because a few steps need a base commit: the template-parity preparation step uses `github.event.pull_request.base.sha` / `github.event.before`, falling back to `git merge-base HEAD origin/main`, and `tests-release-and-log-analysis` needs the PR base SHA. Check out with `fetch-depth: 1`, then fetch exactly what is needed: `git fetch --no-tags --depth=1 origin <base sha or before sha>`, and for the merge-base fallback `git fetch --no-tags --deepen=200 origin main` before calling `git merge-base`. Tests that read history are found by running every shard on the phase PR; each one either gets the commit it needs fetched in its step or keeps `fetch-depth: 0` for that one job with a comment naming the test.

**Phase 2, cancel replaced PR CI.** Add a workflow-level `concurrency` block whose group is per PR for PR-triggered runs (`ci-pr-${{ github.event.pull_request.number }}` with `cancel-in-progress: true`) and unique per run for everything else (`ci-${{ github.run_id }}`, no cancellation), so push CI is never cancelled. If Phase 4 has merged, the same rule applies to the workflows that then carry PR CI (`ci_pr_quick.yml` and the `ci.yml` `workflow_dispatch` runs, keyed by `inputs.pr_number`).

**Phase 3, no empty stall commits.** In both stall-recovery arms, call `_dispatch_review_for_conflicts "${pr_num}" "${head_ref}"` as the first re-trigger step for every stalled PR, not only when the last review run failed or was cancelled. Keep the existing in-flight, wrong-PR, merge-train and capacity guards in front of it. Keep the empty-commit push only as the fallback when the helper returns a non-zero, non-2 code (dispatch failed), and log that case with a new `STALL_RECOVERY_EFFECTIVE_ACTION` value, `retrigger_review_empty_commit_fallback`.

**Phase 4, CI at the end of review.**

1. New caller workflow `.github/workflows/ci_pr_quick.yml` (`name: CI quick`), triggered on `pull_request` `[opened, synchronize, reopened, ready_for_review]` for every base. It has one job, `quick`, that calls `./.github/workflows/ci.yml` through `workflow_call` with `mode: quick`. Its check-runs are named `quick / <job>`, so it never creates a check named `lint`.
2. `ci.yml`:
   - Keep the `push` trigger unchanged.
   - Remove the `pull_request` trigger (PR CI now comes from items 1 and 3). This is a behaviour change, not a §6 rename; it is documented in README, agents and the changelog.
   - Add `workflow_call` with input `mode` (`quick` or `full`, default `full`).
   - Add `workflow_dispatch` with inputs `pr_number` (required) and `head_sha` (optional, for the run name and logs).
   - Add `run-name: CI [pr:<n>]` for dispatch runs, empty otherwise, matching the `[pr:N]` convention other workflows use.
   - In `mode: quick`, only `static-checks` runs and every other job is skipped by `if:`. That is harmless, because those check-runs are named `quick / …`.
   - New job `pr-context` (dispatch runs only) reads `GET pulls/<pr_number>` once and outputs `base_ref`, `base_sha`, `head_sha` and the merge ref. Every expression that reads `github.event.pull_request.*`, `github.base_ref` or `github.event_name == 'pull_request'` gains the dispatch equivalent from `needs.pr-context.outputs`, and checkouts in dispatch runs use `ref: refs/pull/<n>/merge`, so a dispatch run tests the same merge result a `pull_request` run tests today. Check-runs of a dispatch run attach to the branch head SHA, which is the PR head.
3. Final-run trigger, a new helper in `scripts/pr_checks_lib.sh`: `_pr_ensure_final_ci <repo> <pr> <head sha> <head ref>`.
   - It is a no-op unless the new env var `REVIEW_FINAL_CI_WORKFLOW` is non-empty (default empty).
   - Otherwise it makes one `GET actions/workflows/<file>/runs?head_sha=<sha>&per_page=20` read. It does nothing when a run for that SHA (any event, including the `push` run on an `orchestrator/project-*` head) is queued, in progress, or completed `success` or `failure`.
   - Otherwise (no run, or only `cancelled` / `timed_out` / `startup_failure` runs) it dispatches `<file>` on `<head ref>` with `pr_number` and `head_sha`.
   - It logs `REVIEW_FINAL_CI pr= head= outcome=dispatched|exists|disabled|read_failed|dispatch_failed`. A read failure dispatches anyway (fail open, §15), since a duplicate is safe and a missing run blocks the merge.

   Because a `push` run already covers every `orchestrator/project-*` head, those PRs never get a second run. That is the Q41 A deduplication.
4. Callers of the helper:
   - (a) the start of `_pr_wait_for_required_checks` when the required CI check is missing on the head, which covers auto-merge (`review_enable_auto_merge.sh`), the review-blocked judge (`review_rb_judge.sh`) and `deterministic-skip-merge`;
   - (b) the review outcome step in `review_autofix.yml` that posts `ai-review/head-gate` success for a reviewed head, which covers PRs that are not auto-merged;
   - (c) `review_autofix_sweep.yml`, as a safety net for open PRs whose head has `ai-review/head-gate` success but no `ci.yml` run. It uses one runs listing per sweep and reuses the sweep's existing PR read for the head-gate state.
   - `review_autofix.yml` and `review_autofix_sweep.yml` gain a `workflow_call` input `final_ci_workflow` (default `''`), exported as `REVIEW_FINAL_CI_WORKFLOW`. Only `internal-review.yml` and the internal sweep wrapper pass `ci.yml`.
5. Red final CI goes back to review: new job `final-ci-failed` in `ci.yml` (dispatch runs only, `if: failure()`, `needs: lint`) runs new script `scripts/review_final_ci_failed.sh`.
   - It posts `<!-- ai:final-ci-failed:v1 pr=<n> head=<sha> -->` once per head, skipping if that marker already exists.
   - It then dispatches the PR's review workflow with `pr_number`, using the dispatch order `review_freshness_ci_failed.sh` uses.
   - The review collects the failing check-runs as today and the autofix fixes them; its new head then gets its own final run. The helper never re-dispatches CI for a head whose run concluded `failure`, so a head review cannot fix stays red instead of looping.
6. Every other consumer of "a `CI` run on the PR head" keeps working:
   - `AUTO_MERGE_WAIT_WORKFLOWS` (default `CI`) matches dispatch runs.
   - Check-failure triage triggers on `workflow_run` of `CI`, which includes dispatch runs. The implementation must confirm that triage resolves the PR for a `workflow_dispatch` run (whose `pull_requests` array may be empty), from the `[pr:N]` run name or the head branch, and add that resolution if it is missing.
   - `CI quick` does not trigger triage; its failures reach the autofix through the check-run context.

Alternatives rejected:
- Skipping test jobs on intermediate `pull_request` runs inside `ci.yml` leaves a skipped `lint` that counts as passing.
- A label-triggered `pull_request: labeled` run creates a run, with a skipped `lint`, for every unrelated label.
- Dropping the `orchestrator/project-*` push run breaks check-failure triage's base comparison.

## Decisions

### D1 — What runs on each push to a PR

- **Chosen:** `static-checks` only, through `ci_pr_quick.yml`; the full suite once, when review finishes (Q43 A).
- **Alternatives considered:** nothing on intermediate pushes (Q43 B); the full suite on every push (today).
- **Why:** `static-checks` takes about 8 minutes and gives autofix early lint feedback; the test jobs are about 90% of CI's runner time and their results mostly arrive after the review that could use them.

### D2 — Which PRs get CI at the end of review

- **Chosen:** every PR (Q44 B).
- **Alternatives considered:** only PRs the AI review loop manages, with human and session PRs keeping CI on every push (Q44 A).
- **Why:** user decision; `internal-review.yml` reviews every PR, so every PR reaches a terminal review outcome that can start the final run.

### D3 — How stall recovery re-triggers review

- **Chosen:** dispatch the review workflow first; push an empty commit only when the dispatch fails (Q45 A, Q48 A).
- **Alternatives considered:** keep empty commits (Q45 B); no empty-commit fallback at all (Q48 B).
- **Why:** the 74 empty commits in five days each started a full CI run on identical code; the fallback keeps recovery working when the dispatch API fails.

### D4 — Where the orchestrator-branch duplicate CI fix lives

- **Chosen:** inside Phase 4: the final-run helper skips heads that already have a `push` run (Q41 A, Q47 A).
- **Alternatives considered:** a separate phase that drops one of the two runs, or a polling job that mirrors the `push` run's `lint` result (Q47 B).
- **Why:** dropping the `push` run breaks check-failure triage's base comparison, and skipping the `pull_request` run leaves a skipped `lint` that satisfies the required check.

### D5 — How intermediate PR runs avoid creating a passing `lint`

- **Chosen:** a separate caller workflow (`CI quick`) whose check-runs are named `quick / …`, and no `pull_request` trigger on `ci.yml`.
- **Alternatives considered:** skipping test jobs inside `ci.yml` on intermediate runs; a `pull_request: labeled` trigger for the final run.
- **Why:** GitHub reports a job skipped by `if:` as successful for required checks, so both alternatives could let an untested head merge.

### D6 — Shallow-checkout scope

- **Chosen:** the three jobs with `fetch-depth: 0` (`tests-system-python`, `tests-hooks-and-orchestrator`, `tests-release-and-log-analysis`) (Q41 B, Q46 A).
- **Alternatives considered:** only the `tests-system-python` shards (Q46 B).
- **Why:** the other two jobs have the same 3.5-minute checkout; the change and its verification are identical.

## Phases & Merge Strategy

Each phase is one PR, independently mergeable in any order and production-safe at merge.

1. **Shallow checkouts in slow CI jobs.**
   - Files: `.github/workflows/ci.yml` (the three jobs' checkout and parity-preparation steps), tests that pin those steps.
   - Done: on the phase PR's own CI run, all shards pass and the median checkout of the three jobs is 0.5 min or less.
   - Rollback: revert the PR.
2. **Cancel replaced PR CI.**
   - Files: `ci.yml` (and `ci_pr_quick.yml` if Phase 4 merged first), tests.
   - Done: a test pins the concurrency expressions, and a second push to a test PR cancels the first run while a `main` push run is never in a cancelling group.
   - Rollback: revert the PR.
3. **Stall recovery dispatches review.**
   - Files: `scripts/orchestrate_poll_process.sh` (both arms), its tests, an `agents.d/` fragment.
   - Done: tests show the dispatch is tried first and the empty commit appears only on dispatch failure.
   - Rollback: revert the PR.
4. **One full CI run per PR at the end of review.**
   - Files: listed under Files & Modules.
   - Done:
     - on a test PR, intermediate pushes run only `CI quick / static-checks`;
     - the head review finishes with gets exactly one `CI [pr:N]` run, with the full suite on the merge ref;
     - an `orchestrator/project-*` PR gets no dispatch run;
     - a failing final run posts the marker and re-dispatches review once;
     - no PR head carries a `lint` check-run unless the full suite ran.
   - Rollback: revert the PR. The `pull_request` trigger comes back, and the inert hooks stay harmless.

## Implementation Steps

Phase 1:
1. In `.github/workflows/ci.yml`, change `fetch-depth: 0` to `fetch-depth: 1` in `tests-system-python` (~line 3541), `tests-hooks-and-orchestrator` (~line 584) and `tests-release-and-log-analysis` (~line 2124).
2. In each of those jobs, before the first step that reads a base or `before` SHA, add a step that fetches that SHA with `--depth=1` when it is a 40-hex commit. Make the parity step's `merge-base` fallback first run `git fetch --no-tags --deepen=200 origin main`.
3. Push the phase PR, read every shard's log for git errors (`unknown revision`, `shallow`, `merge-base`), and fix each failing test's step by fetching what it needs. If a job still needs full history, restore `fetch-depth: 0` for that job alone with a comment naming the test.
4. Update the comments above the three checkouts, the tests that pin them (search `tests/` for `fetch-depth`), and add `changelog.d/<issue>-ci-shallow-checkouts.md`.

Phase 2:
1. Add the workflow-level `concurrency` block to `ci.yml` as described in Approach (and to `ci_pr_quick.yml` / `ci.yml` dispatch runs if Phase 4 is on `main`).
2. Add a test (new `tests/test_ci_concurrency.py`) that loads the workflow YAML. It asserts that the PR group is keyed by PR number with `cancel-in-progress: true`, and that the push path uses a per-run group with no cancellation.
3. Add `changelog.d/<issue>-ci-cancel-replaced-pr-runs.md`.

Phase 3:
1. In the orchestrator arm (~lines 20470–20640 of `scripts/orchestrate_poll_process.sh`), move the `_dispatch_review_for_conflicts` call out of the `if [ -n "${_rtr_failed_conclusion}" ]` branch so it runs for every stalled PR that passes the existing guards. Keep rc `0` → `redispatch_review_autofix` and increment, and rc `2` → `redispatch_review_autofix` without increment. On any other rc, log and fall through to the existing guarded empty-commit block, setting `STALL_RECOVERY_EFFECTIVE_ACTION="retrigger_review_empty_commit_fallback"` when that push succeeds.
2. Apply the same change to the standalone arm (~lines 26090–26200).
3. Extend the stall-recovery tests (search `tests/` for `stall recovery: re-trigger review` and `redispatch_review_autofix`) for:
   - dispatch-first with a successful last run;
   - rc 2 skip;
   - dispatch failure leading to the empty-commit fallback;
   - the in-flight guard still blocking both.
4. Add an `agents.d/` fragment for the Stall recovery section and `changelog.d/<issue>-stall-recovery-dispatch-review.md`.

Phase 4:
1. Add `workflow_call` (`mode` input) and `workflow_dispatch` (`pr_number`, `head_sha`) to `ci.yml`, plus `run-name`, and remove its `pull_request` trigger. Update the header comment that explains the triggers (#7283, #6726, #7042, Q45 context).
2. Add the `pr-context` job and route every `github.event.pull_request.*`, `github.base_ref` and `github.event_name == 'pull_request'` expression in `ci.yml` through a dispatch-aware equivalent. Make checkouts in dispatch runs use `refs/pull/<n>/merge`, falling back to the head with a `::warning::` when the merge ref is missing. Add `if: inputs.mode != 'quick'` (true for push and dispatch runs) to every job except `static-checks`.
3. Add `.github/workflows/ci_pr_quick.yml` as described.
4. Add `_pr_ensure_final_ci` to `scripts/pr_checks_lib.sh` with the docstring §15 asks for (inputs, outputs, one read plus at most one dispatch, fail-open), and call it at the start of `_pr_wait_for_required_checks` when the required CI check is missing.
5. Add the `final_ci_workflow` input to `review_autofix.yml` and `review_autofix_sweep.yml`, export it as `REVIEW_FINAL_CI_WORKFLOW`, and call the helper from the step that posts `ai-review/head-gate` success and from the sweep. Pass `final_ci_workflow: ci.yml` from `internal-review.yml` and the internal sweep wrapper.
6. Add the `final-ci-failed` job to `ci.yml` and the new `scripts/review_final_ci_failed.sh`, modelled on `scripts/review_freshness_ci_failed.sh`.
7. Confirm or add PR resolution for `workflow_dispatch` CI runs in check-failure triage.
8. Update the README sections on CI triggers and the review/merge flow in place (§30.B). Add an `agents.d/` fragment covering:
   - the new flow;
   - the `REVIEW_FINAL_CI` and `final-ci-failed` log lines in "Stable log prefixes (contractual)";
   - the marker.

   Add `changelog.d/<issue>-ci-once-at-end-of-review.md`.
9. Check `wc -c` on `ci.yml` and `review_autofix.yml` (§27).

## Files & Modules

- `.github/workflows/ci.yml` (Phases 1, 2, 4)
- `.github/workflows/ci_pr_quick.yml` [new] (Phase 4)
- `.github/workflows/review_autofix.yml`, `.github/workflows/review_autofix_sweep.yml`, `.github/workflows/internal-review.yml`, the internal sweep wrapper (Phase 4)
- `scripts/pr_checks_lib.sh` (Phase 4)
- `scripts/review_final_ci_failed.sh` [new] (Phase 4)
- `scripts/check_failure_triage.sh` / `scripts/check_triage_resolution.py` (Phase 4, only if dispatch-run PR resolution is missing)
- `scripts/orchestrate_poll_process.sh` (Phase 3)
- `tests/test_ci_concurrency.py` [new], plus updated or new tests next to the existing CI-structure, stall-recovery, pr-checks and review-freshness tests
- `README.md` (in-place corrections), `agents.d/<issue>-*.md` [new], `changelog.d/<issue>-*.md` [new], one per phase

## Data Model / Index Changes

None.

## Tests

- Phase 1: the phase PR's own CI run is the end-to-end test. Every shard must pass, and checkout medians are read from the job steps.
- Phase 2: a unit test on the parsed YAML. On the phase PR, two quick pushes must show the first run `cancelled`.
- Phase 3: unit tests with the existing fake `gh` / git harness for every rc path, plus both arms' in-flight guards.
- Phase 4:
  - Unit tests:
    - `_pr_ensure_final_ci` covering no run, a queued run, a completed `success` run, a completed `failure` run, only cancelled runs, a read failure and disabled;
    - `review_final_ci_failed.sh` with the marker present and absent;
    - YAML tests that `ci_pr_quick.yml` never produces a job named `lint` at top level, that every non-`static-checks` job in `ci.yml` skips in quick mode, and that no `ci.yml` trigger is `pull_request`.
  - End to end on a test PR, the Phase 4 done condition above.
- Every phase also runs the existing CI-structure tests (`tests/` files that pin `ci.yml` jobs, budgets and wiring), `python3 scripts/assemble_agents.py check --repo-root .` and the §27 size test.

## Risks & Mitigations

- **A skipped `lint` could satisfy the required check.** Mitigated by design: PR-triggered quick runs live in `ci_pr_quick.yml`, whose checks are `quick / …`, and `ci.yml` no longer runs on `pull_request`. A Phase 4 YAML test pins both.
- **CI failures surface only at the end of review.** ACCEPTED (Q43 A keeps `static-checks` per push). The final run's failure re-enters review once per head through `final-ci-failed`.
- **PRs that review never finishes never get full CI.** Mitigated by the sweep safety net (Phase 4 item 4c), and the merge paths dispatch through `_pr_wait_for_required_checks`. Human PRs get full CI once review posts `ai-review/head-gate` success (Q44 B).
- **Dispatch runs test the merge ref, but their checks attach to the branch head.** That is the same SHA a `pull_request` run attaches to today, so required checks behave the same.
- **A history-dependent test breaks under shallow checkout.** ACCEPTED, pending the Phase 1 PR's shard run: that job keeps `fetch-depth: 0` with a comment.
- **Dispatch-first stall recovery might re-review a head whose last review passed.** That matches what the empty commit does today, without the extra CI run and without flipping the stale-base gate of a running review.
- **Phases 2 and 4 both touch `ci.yml` triggers and concurrency.** Each phase's steps say how to apply it with or without the other on `main`.

## Rollout

Each phase ships to this repo on merge. Phases 1–3 need no settings. Phase 4 also takes effect on merge for this repo, because `internal-review.yml` passes `final_ci_workflow: ci.yml`. Consumers receive the inert hooks on the next `@stable` sync and are unaffected. Rollback per phase: revert its PR. The expected effect is visible in the queue analysis (CI runner minutes per 12 h) within a day of each merge.

## §18.E Automation

- Scripts: Phase 4 adds `scripts/review_final_ci_failed.sh`. It is not a manual script; it runs from the new `final-ci-failed` job in `.github/workflows/ci.yml` (trigger: `workflow_dispatch` CI runs that fail). Everything else extends existing scripts and workflows.
- Scheduler / entry points:
  - `ci.yml` (`push`, `workflow_dispatch`, `workflow_call`) and `ci_pr_quick.yml` (`pull_request`);
  - `review_autofix.yml` via `internal-review.yml`;
  - `review_autofix_sweep.yml` (existing 30-minute schedule);
  - `orchestrate_poll.yml` via the internal poller for Phase 3.
- Long-running supervisor: none.
- DB work: none.
- `docs/scripts-pending-removal.md`: no entry; `review_final_ci_failed.sh` is a permanent part of the review flow, not single-use or long-running.

## References

- Runner-time measurements: this session's analysis, 2026-11-15 (job timings of 3,271 jobs that started 17:00–05:00 UTC).
- Issue #9517 (15-minute crons), closed as not planned after the measurement.
- Release-gate runner-wait fix: Refs #9480.
- Required CI check before auto-merge (#6963); cancelled checks block, newest run per name (#7127); freshness CI-failed re-dispatch (Q103, `scripts/review_freshness_ci_failed.sh`); PR CI for every base (#7283).
