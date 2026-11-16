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
- Item 4: `ci.yml` skips its test jobs when every changed file is on a list of paths no test reads, and `lint` still reports a real result.
- Item 6: three kinds of purely informational bot comments no longer start the four `issue_comment` wrappers.
- Item 7: a consumer's `AI Check Failure Triage` is no longer created for runs on the default branch.
- Item 8: the poller cron runs every 15 minutes and the closed-PR sweep runs hourly, here and in consumers.
- Item 9: the review sweep no longer treats a review run that is merely waiting for a runner as wedged.
- Item 12: the one-off `check-failure-triage-historical.yml` and its references are removed.

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
4. **PR (d): item 4.** `ci.yml`, built on top of #9528's merged `ci.yml`.

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

**Item 4.** A `changes` job computes the changed files (the PR's base...head, or a push's before...after). An empty, unreadable or all-zero base means "code changed". The safe list is the set of paths no test or CI step reads:

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

The test jobs run unless `changes` says code did not change. `lint` requires `success` from every job as today, except that the gated test jobs may be `skipped` when `changes.outputs.code == 'false'`. `static-checks` always runs.

## Tests

- Item 7: the template trigger and the job condition (`tests/test_ci_failure_workflow_run_triggers.py`).
- Item 8: the cron pins (`tests/test_review_merge_train.py`), plus the existing poller, follow-up and wrapper tests.
- Items 3 and 9: fake-`gh` tests for every throttle reason and fail-open path, and for the sweep's zero-jobs rule.
- Item 12: the full test set that references the file, run before and after the removal.
- Item 6: per-kind tests that the call uses the job token, and that readers still find the comment.
- Item 4: workflow-structure tests for the gate, plus a matrix over `lint`'s decision logic.

## Risks & Mitigations

- Later conflict detection on integration branches (item 3): the PR-mergeable trigger and the 24-hour cap still surface conflicts, and `INTEGRATION_SYNC_MIN_INTERVAL_SECS=0` restores the old behaviour.
- A skipped test job satisfying a required check (item 4): `lint` runs with `always()` and its own explicit rules, and unknown diffs run everything.
- Slower recovery of orphaned runs on conflicted closed PRs (item 8): bounded at one hour.

## Rollout

Each PR is effective on merge in this repository. Consumers pick up the template, poller and sweep changes on the next `@stable` sync (`.github/workflows/update_workflows.yml`, daily 04:00 UTC plus the release dispatch). Rollback is a revert.

## §18.E Automation

- No new script, supervisor or scheduled workflow; every change edits an existing workflow, template or the poller.
- Entry points: `.github/workflows/internal-orchestrate-poll.yml` (cron) via `orchestrate_poll.yml`; `.github/workflows/internal-cancel-on-pr-close.yml` (cron and `pull_request: closed`); `.github/workflows/review_autofix_sweep.yml` (cron); `.github/workflows/ci.yml` (`push` / PR); consumer wrappers via `update_workflows.yml`.
- No long-running supervisor is needed.
- No DB work.
- `docs/scripts-pending-removal.md`: no new entry; item 12 deletes one.

## References

- `docs/plans/cut-ci-runner-time-plan.md` (orchestrator project #9528, Refs #9528)
- Issue #9517 (the earlier cron proposal, closed as not planned)
