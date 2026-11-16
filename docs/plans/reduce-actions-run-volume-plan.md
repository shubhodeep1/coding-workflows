# Reduce GitHub Actions run volume across coding-workflows and its consumers

## Summary

Actions for this account has been jammed since 2026-11-11. On 2026-11-15 coding-workflows had 487 queued runs, and its 13 consumers had 5 to 35 each. This plan cuts the runs and runner time that the pipeline creates without dropping any work. It complements orchestrator project #9528 (`docs/plans/cut-ci-runner-time-plan.md`), which changes how `ci.yml` runs on PRs. The work is done in an interactive session at the user's request, as four PRs.

## Context

Measured 2026-11-12 to 2026-11-15 from the Actions REST API (coding-workflows, hourly windows) and from samples of `digital_pa` and `tele-funtoken-msg-scoring`:

| The numbers that matter | Value |
| --- | --- |
| Runs created in coding-workflows per day | 8,745 / 6,460 / 3,590 / 4,331 |
| Share of those that were `skipped` | 80% / 78% / 68% / 72% |
| Runs started by `issue_comment` (4 wrappers per comment) | 6,488 / 4,624 / 2,104 / 2,695 |
| `CI` runs per day (37 jobs, about 400 runner-minutes each) | 244 / 187 / 134 / 149 |
| …of which on `orchestrator/project-*` | 81 / 69 / 52 / 56 |
| Pushes to `orchestrator/project-8969` on 2026-11-12 | 13, all `chore: sync main into orchestrator/project-8969` |
| Consumer runs per day (13 repos, 2026-11-14) | about 12,000 |
| `digital_pa` runs on 2026-11-14 | 624, of which 312 skipped `AI Check Failure Triage` (`workflow_run`) and 271 cron runs cancelled while waiting |
| `digital_pa` scheduled run: work vs wall clock | 0.4 min of work, 331 min from creation to finish |
| Review sweep dispatches replaced while pending (2026-11-12) | 86 |
| `check-failure-triage-historical.yml` runs per day | about 12 (one per push to `main`), all skipping |

Runner time is what jams the account. Skipped runs take no runner, but they flood the queue and the API, and in consumers each one fires another `workflow_run` event.

## Goals

- Item 3: an integration branch is synced from `main` only when it must be (its PR cannot merge, a wave is about to be dispatched, the final merge is next), or at most once every 24 hours.
- Item 4: a PR's final CI run (`ci.yml` `workflow_dispatch`) skips its test jobs when every changed file is on a list of paths no test reads, and `lint` still reports a real result.
- Item 6: three kinds of purely informational bot comments no longer start the four `issue_comment` wrappers.
- Item 7: a consumer's `AI Check Failure Triage` is no longer created for runs on the default branch.
- Item 8: the poller cron runs every 15 minutes and the closed-PR sweep runs hourly, here and in consumers.
- Item 9: the review sweep no longer treats a review run that is merely waiting for a runner as wedged.
- Item 12: the one-off `check-failure-triage-historical.yml` and its references are removed.
- Item 10: `Integration PR readiness check` takes a runner on a label change only when the label is `ai:override-incomplete-merge`.
- Item 14: push CI keeps at most one running and one waiting run per branch; a newer push replaces the waiting run.

## Non-goals

- Item 2 (duplicate `push` + `pull_request` CI on `orchestrator/project-*`): delivered by #9528 Phase 4 (decision D4 there).
- A single `issue_comment` router workflow. Run path and name are read by the poller's concurrency caps (`scripts/orchestrate_lib.py`), heal provenance (`scripts/workflow_failure_heal.py`), `scripts/unblock_ledger.py`, the hardened approval check in `workflow-templates/ai-implement.yml`, and the release e2e matchers. The sync also never deletes old wrappers.
- Moving comments that readers trust by author login (attempt journal, autofix editor summary, orchestrator state, merge-base sync, phase markers) off the PAT.
- Runner capacity (plan, larger or self-hosted runners) and ghost-run cleanup.

## Constraints

- §6: no identifier is renamed or removed except the item 12 file, its test and its registry entry, which the user asked for.
- §4: every new env var has a default.
- §14 / §20 / §30: template changes reach consumers on the next `@stable` sync; each PR carries a `changelog.d/` fragment; new agents.md text goes through `agents.d/`; wrong existing text is corrected in place.
- §15: new API reads are bounded and fail open.
- §27: workflow files are checked with `wc -c`.

## Decisions

User answers, 2026-11-16. Q9 to Q11 were taken as clear-winner assumptions and not overridden.

### D1 — Duplicate integration-branch CI (item 2)

- **Chosen:** leave it to #9528 Phase 4 (Q1 A).
- **Alternatives considered:** a standalone fix now.
- **Why:** Phase 4 already removes it (D4 there), and a second change to the same `ci.yml` triggers would conflict.

### D2 — When to sync main into an integration branch (item 3)

- **Chosen:** sync on need, capped at once every 24 hours (Q2 A).
- **Alternatives considered:** sync on need with a 6-hour cap; sync on need with no cap.
- **Why:** it stops the sync-per-main-push churn and still surfaces conflicts within a day.

### D3 — Consumer scope of items 3 and 8

- **Chosen:** the new defaults apply in every consumer (Q3 A).
- **Alternatives considered:** this repository only, with a variable opt-in for consumers.
- **Why:** the runner pool is shared across the account.

### D4 — Comment fan-out (item 6)

- **Chosen:** only the audited comment kinds, switched per call (Q4 A). Four were proposed; the reader audit kept security-pass results on the PAT (see Approach), so three moved.
- **Alternatives considered:** also add a router workflow; drop item 6.
- **Why:** see Non-goals; most comment kinds are trusted by author login.

### D5 — Cron cadence (item 8)

- **Chosen:** poller `*/15`, sweep hourly (Q5 A).
- **Alternatives considered:** `*/10` and `*/30`; `*/30` and hourly.
- **Why:** the follow-up poll after progress keeps active projects moving.

### D6 — Sweep backpressure (item 9)

- **Chosen:** fix only the wedged-run check (Q6 A).
- **Alternatives considered:** also skip all sweep dispatches above a queue depth.
- **Why:** dedupe alone removes the churn without delaying any review.

### D7 — PR split

- **Chosen:** four PRs (Q7 A).
- **Alternatives considered:** one PR per item.
- **Why:** every push to a PR starts about six runs.

### D8 — Merging

- **Chosen:** merge each PR after clean review rounds, and stop and ask if the required checks stay stuck in the queue (Q8 A).
- **Alternatives considered:** open the PRs for the user to merge.
- **Why:** user decision.

### D9 — Plan location

- **Chosen:** this file (Q9 A).
- **Alternatives considered:** chat only.
- **Why:** repository convention for plans.

### D10 — Item 4 timing

- **Chosen:** after #9528 merges (Q10 A).
- **Alternatives considered:** build now and rebase later.
- **Why:** #9528 Phases 1, 2 and 4 rewrite the same `ci.yml` regions.

### D11 — Consumer triage filter (item 7)

- **Chosen:** `branches-ignore: [main, master]` (Q11 A).
- **Alternatives considered:** each repository's CI workflow names, rendered at sync time.
- **Why:** it keeps one attested, byte-identical file in every consumer; per-repository rendering would need release-manifest changes.

### D12 — How a final run learns it is docs-only (item 4)

- **Chosen:** the dispatcher passes `docs_only=true`, and `lint` re-checks the merged tree (Q1 A, 2026-11-16).
- **Alternatives considered:** a `changes` job that every test job waits on; dropping item 4.
- **Why:** a `changes` job puts one more runner wait in front of every final run's test jobs and breaks the "worker jobs start together" contract in `tests/test_ci_job_split_contract.py`. The dispatcher already runs just before the dispatch, and `lint` runs last anyway, so the check adds no wait.

### D13 — Push runs are not gated (item 4)

- **Chosen:** only final-CI `workflow_dispatch` runs (Q2 A, assumed 2026-11-16).
- **Alternatives considered:** also gate pushes and teach triage and heal to ignore runs with skipped tests.
- **Why:** `scripts/check_failure_triage.sh`'s base gate and heal's `HEAL_STALE_GREEN_RUNS` both read push-run conclusions; a green docs-only push would hide a broken base.

### D14 — Push CI coalesces per branch (item 14)

- **Chosen:** push runs share the concurrency group `ci-push-<ref>` with `cancel-in-progress: false` (Q3 A, 2026-11-16).
- **Alternatives considered:** keep one group per run (every pushed commit gets its own 37-job run); cancel the running push run too.
- **Why:** on 2026-11-16 four `main` push runs (148 jobs) sat queued at once, and every commit had already passed its PR's own CI. The running run is never cancelled, so check-failure triage's base gate (`workflow_runs[0]` of the base's push runs) always reads a run that finishes. A replaced run ends `cancelled`, which `internal-check-failure-triage.yml` and `workflow-failure-heal-intake.yml` ignore, and `scripts/ci_cancelled_rerun.py` never re-runs, because the replaced commit is never a PR's current head. Heal's stale auto-close (`heal_stale_decision` in `scripts/workflow_failure_heal.py`) used to keep a heal issue open for any post-anchor push run that was not `success`; it now skips `cancelled` runs, which count neither as green nor as red (Q5 A, 2026-11-16). Without that, one replaced run would keep a CI heal issue open until the next occurrence.

### D15 — Readiness check filters label events (item 10)

- **Chosen:** a job-level `if` that skips `labeled` / `unlabeled` events for every label except `ai:override-incomplete-merge` (Q4 A, 2026-11-16).
- **Alternatives considered:** drop the `labeled` / `unlabeled` triggers; keep running on every label.
- **Why:** `scripts/check_integration_pr_readiness.py` reads no other PR label, a label change never moves the head SHA, so the status already posted for it stands, and the poller refreshes the status itself when the tracking issue changes (`TRACKING_BODY_READINESS_REFRESH`).

## Phases & Merge Strategy

1. **PR (a): items 7 and 8.** Files:
   - `workflow-templates/ai-check-failure-triage.yml`
   - `workflow-templates/ai-orchestrate-poll.yml`
   - `workflow-templates/ai-cancel-on-pr-close.yml`
   - `.github/workflows/internal-orchestrate-poll.yml`
   - `.github/workflows/internal-cancel-on-pr-close.yml`
   - tests and docs
2. **PR (b): items 3, 9 and 12.** Files:
   - `scripts/orchestrate_poll_process.sh`
   - `.github/workflows/review_autofix_sweep.yml`
   - removal of `.github/workflows/check-failure-triage-historical.yml` and `tests/test_check_failure_triage_historical.py`
   - registry, inventory and docs
3. **PR (c): item 6.** The posting sites of the three comment kinds that passed the reader audit, changed per call.
4. **PR (d): item 4.** `ci.yml`, `scripts/pr_checks_lib.sh` (`_pr_ensure_final_ci`) and the new `scripts/ci_docs_only_paths.sh`, built on top of #9528's `ci.yml` (#9563).
5. **PR (e): items 10 and 14.** `ci.yml`'s concurrency block, `integration-pr-readiness.yml`, heal's stale auto-close window (`scripts/workflow_failure_heal.py`), and their tests.

Each PR is independently revertable.

## Approach

**Item 7.** The `workflow_run` trigger of the consumer triage template gets `branches-ignore: [main, master]`. The `triage-workflow-run` job only triages runs whose event is `pull_request`, and those run on the PR head branch. The file stays byte-identical in every consumer, so the release-manifest sha256 attestation is unchanged.

**Item 8.** The poller cron goes from `*/5` to `*/15`. The poller already queues a follow-up run after any tick that moves a project forward (`POLL_FOLLOW_UP_ON_PROGRESS_ENABLED`), and GitHub was running the `*/5` cron every 25 to 60 minutes anyway. The closed-PR sweep cron goes from `*/5` to `23 * * * *`. The merge-train release it also runs happens on every poll tick and every PR close.

Effects:

- A closed PR with merge conflicts can keep orphaned runs for up to an hour.
- The Claude pool-health Telegram note, which is sent only from a cycle in the first `CLAUDE_POOL_HEALTH_WINDOW_MINUTES` of the hour, goes out in fewer hours.
- Capacity resume keeps its "an hour since the last pass" fallback.

**Item 3.** `sync_default_into_integration_branch` gains a throttle in front of the merges API call. Main is merged in when any of these holds:

- the integration (final) PR reports `mergeable == false`;
- a new wave is about to be dispatched;
- the finalizer is about to merge;
- the last successful sync is older than `INTEGRATION_SYNC_MIN_INTERVAL_SECS` (default `86400`; `0` restores the sync on every tick).

Otherwise the tick logs `INTEGRATION_SYNC_THROTTLE outcome=skip` and continues. A failed or unreadable check fails open to syncing.

**Item 9.** The sweep keeps treating a run queued longer than `SWEEP_STALE_QUEUED_MINUTES` as a candidate. It now replaces the run only when GitHub created no jobs for it: one `GET actions/runs/<id>/jobs?per_page=1` per candidate, and an unreadable answer keeps today's behaviour. A review run waiting for a runner is never replaced, and it reviews the PR's current head when it starts, so no review is lost.

**Item 12.** The removal preflight comes from `docs/scripts-pending-removal.md`:

- issues #6932 and #7300 are closed and link run 37922279826;
- the latest run ran only the `guard` job.

The workflow, its test, its `ci.yml` test-list line, its registry entry, its inventory line and the agents.md text are removed together.

**Item 6.** Each `gh` call that posts one of these comment kinds uses the job's `GITHUB_TOKEN` for that call only, so `gh api user` identity lookups stay on the PAT:

- the materiality advisory;
- the review-blocked judge decision and hold notices;
- the security-audit tracker report.

A kind is moved only after its readers are confirmed not to filter by author. Security-pass results were the fourth candidate, and they failed that audit: the next pass trusts its own earlier markers only when the PAT login wrote them (`SECURITY_PASS_AUTHOR_LOGIN`), so they stay on the PAT. The fallback to the PAT happens only when GitHub refuses the job token (401/403). A POST is sent once, and any other failure is not resent, because a timeout may already have created the comment.

**Item 4.** The final-CI dispatcher (`_pr_ensure_final_ci`) reads the PR's changed files once, just before it dispatches, and passes `docs_only=true` when every file (and every renamed-from path) is on the list in `scripts/ci_docs_only_paths.sh`. An unreadable listing, an empty one, a full first page (100 files) or a missing classifier means "code changed". The safe list is the set of paths no test or CI step reads:

- `analysis/**`
- `docs/plans/**`
- `docs/postmortems/**`
- `docs/completed/**`
- `docs/deploy-activation/**`
- `docs/triage/**`
- `docs/how-it-works.md`
- `docs/hardenaireviewprompt.md`
- `docs/changelog-style.md`
- `docs/*-future-improvements.md`

The seven test jobs skip when `inputs.docs_only == 'true'`; `static-checks` always runs. The test jobs keep no `needs`, so they still start together. In a docs-only run `lint` checks out `refs/pull/<n>/merge` (depth 2) and runs `scripts/ci_docs_only_paths.sh verify`: the merge's second parent must be the run's head and every file it changes against the base must be on the list. Only after that check passes may the seven named jobs be `skipped`; every other job, and every other result, still needs `success`. A wrong input therefore fails `lint`. A branch whose `ci.yml` predates the input refuses the dispatch, and the dispatcher retries once without it.

Push runs (`main`, `stable`, `orchestrator/project-*`) are never gated: check-failure triage reads the newest push run on a PR's base as the base result, and heal's stale auto-close counts green push runs, so a docs-only push that skipped the tests would make a broken branch look green to both.

**Item 14.** `ci.yml`'s concurrency group gains a `push` arm, `format('ci-push-{0}', github.ref)`, ahead of the per-run fallback; `cancel-in-progress` is unchanged and stays `false` for pushes. GitHub keeps one running and one pending run per group, so pushes to the same branch now run one after another instead of side by side, and only the newest waiting commit is tested. Each branch has its own group, so `main`, `stable` and every `orchestrator/project-*` branch are independent.

**Item 10.** The `readiness` job runs on `opened`, `synchronize`, `reopened` and `ready_for_review` as before, and on `labeled` / `unlabeled` only for `ai:override-incomplete-merge`. A skipped job takes no runner, and its check run reports `skipped`.

## Tests

- Item 7: the template trigger and the job condition (`tests/test_ci_failure_workflow_run_triggers.py`).
- Item 8: the cron pins (`tests/test_review_merge_train.py`), plus the existing poller, follow-up and wrapper tests.
- Items 3 and 9: fake-`gh` tests for every throttle reason and fail-open path, and for the sweep's zero-jobs rule.
- Item 12: the full test set that references the file, run before and after the removal.
- Item 6: per-kind tests that the call uses the job token, and that readers still find the comment.
- Item 4: `tests/test_ci_docs_only_final_ci.py`: the path list, `verify` against real merge commits, the workflow structure, a matrix over `lint`'s decision logic, and the dispatcher against a fake `gh`.
- Item 14: `tests/test_ci_concurrency.py` evaluates the group for push runs on `main`, `stable` and an integration branch, and pins that the running push run is never cancelled; `tests/test_workflow_failure_heal.py` pins that heal's stale auto-close skips `cancelled` runs without counting them as green.
- Item 10: `tests/test_check_integration_pr_readiness.py` pins the job condition and checks its label matches the script's `OVERRIDE_LABEL`.

## Risks & Mitigations

- Later conflict detection on integration branches (item 3): the PR-mergeable trigger and the 24-hour cap still surface conflicts, and `INTEGRATION_SYNC_MIN_INTERVAL_SECS=0` restores the old behaviour.
- A skipped test job satisfying a required check (item 4): `lint` runs with `always()`, verifies the merged tree itself before it accepts `skipped`, and anything the dispatcher cannot read runs everything.
- Slower recovery of orphaned runs on conflicted closed PRs (item 8): bounded at one hour.
- Intermediate commits on a branch get no push CI of their own (item 14): each passed its PR's CI before it merged, and the newest commit is always tested. A breakage introduced by one of them surfaces in the next run that does start. Heal's `HEAL_STALE_GREEN_RUNS` counts fewer green push runs, so its stale auto-close can take longer.
- Pushes to one branch now wait for that branch's running CI (item 14): the newest waiting commit starts as soon as the running run finishes.
- A run cancelled mid-failure is skipped by heal's stale auto-close (item 14, Q5 A): a failure that comes back posts an occurrence comment, which moves the anchor and keeps the issue open.
- The release CI gate (`test-and-mark-stable.yml` `ci_lint_gate`, `scripts/release_ci_gate.sh`) needs `lint` on the exact `stable` SHA. If two pushes reach `stable` while its CI runs, the middle commit gets no run, and a release dispatched for it waits out the gate and fails closed; the `RELEASE_STALE_TIP` guard rejects that commit anyway, and the next dispatch targets the newer tip.

## Rollout

Each PR is effective on merge in this repository. Consumers pick up the template, poller and sweep changes on the next `@stable` sync (`.github/workflows/update_workflows.yml`, daily 04:00 UTC plus the release dispatch). Rollback is a revert.

## §18.E Automation

- No new script, supervisor or scheduled workflow; every change edits an existing workflow, template or the poller.
- Entry points: `.github/workflows/internal-orchestrate-poll.yml` (cron) via `orchestrate_poll.yml`; `.github/workflows/internal-cancel-on-pr-close.yml` (cron and `pull_request: closed`); `.github/workflows/review_autofix_sweep.yml` (cron); `.github/workflows/ci.yml` (`push` / PR); `.github/workflows/integration-pr-readiness.yml` (`pull_request`); consumer wrappers via `update_workflows.yml`.
- No long-running supervisor is needed.
- No DB work.
- `docs/scripts-pending-removal.md`: no new entry; item 12 deletes one.

## References

- `docs/plans/cut-ci-runner-time-plan.md` (orchestrator project #9528, Refs #9528)
- Issue #9517 (the earlier cron proposal, closed as not planned)
