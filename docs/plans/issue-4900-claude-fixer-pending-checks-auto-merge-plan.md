# Claude-fixer: auto-merge a clean review once its pending checks finish green

Source issue: shubhodeep1/coding-workflows#4900 (https://github.com/shubhodeep1/coding-workflows/issues/4900)
Base branch: main
Security pass: run

## Summary

In Claude-fixer mode, a `claude/*` PR whose reviewer panel finishes before CI does gets a `kind=findings` hand-off with 0 ledger entries and never auto-merges. This plan replaces that hand-off with a distinct "pending checks" marker, and lets the hourly `claude-pr-catch-all` sweep enable head-bound auto-merge once the head's check runs have all completed without a failure, with no reviewer re-run, no Claude session, and no human.

## Context

- `scripts/review_autofix_step_claude_fixer_handoff.sh:117-154`: a clean ledger refreshes the check snapshot with `scripts/collect_pr_check_runs_context.py` (it waits up to `CHECK_RUNS_WAIT_TIMEOUT_SECS`, default 300 s, then reports `collection_status: timeout`) and exports `CLAUDE_FIXER_ZERO_FINDINGS=true` only for `collection_status: ready`, `failed_count: 0`, `incomplete_count: 0`. Anything else falls through to the `kind=findings` hand-off (lines 162-194).
- `.claude/scripts/check_in_status.py` (`_check_claude_fixer_pr`) turns that hand-off into `review-round` → `hand_back_fixer` / `next_stage review`, so a fixer is woken with nothing to fix; without `CLAUDE_FIXER_VERDICT_BOT_LOGIN` the only exit is a hold and a human merge.
- Observed on PR #4869 (head `e35d1fe`, run 36501676228): the 6 reviewers reported nothing at 00:51:30Z, CI `lint` finished `success` at 00:55:09Z. CI `lint` often runs ~45 min (#4707), so this is the common path.
- The review gate (`.github/workflows/review_autofix.yml`, "Evaluate review gate") skips dispatched re-runs on a head that carries a hand-off (`claude_fixer_awaiting_session`). The 30-minute `sweep` job of `.github/workflows/review_autofix_sweep.yml` dispatches `internal-review.yml` for every open PR, so a head without a hand-off would get the whole reviewer panel again every 30 minutes.
- The hourly `claude-pr-catch-all` job (`scripts/claude_pr_sweep.py`) already walks every open `claude/*` PR in this repo and every consumer in `.github/ai/consumer_repos.json` with `GH_PAT`, and already reads each PR through `check_in_status.check_pr_hand_back`.
- Related: #4835 (a stalled reviewer slot, same symptom), #4707 (faster CI).

## Goals

- G1. A clean ledger whose refreshed, same-head, well-formed snapshot has `failed_count: 0` and `incomplete_count > 0` posts a pending-checks comment (`<!-- ai:claude-fixer-pending-checks:v1 head=<sha> round=<n> ledger=<sha256> -->`), never a `kind=findings` hand-off.
- G2. The hourly `claude-pr-catch-all` sweep enables head-bound auto-merge (`scripts/review_enable_auto_merge.sh`, `--match-head-commit`) for a PR whose current head carries a trusted pending-checks marker, once a fresh snapshot of that head is `ready` with at least one check run, 0 failed and 0 incomplete, and the review run the marker links completed with `success`. It never re-runs the reviewers.
- G3. Fail closed: a failed check, a malformed or missing snapshot, a moved head, an untrusted or superseded marker, a blocking label, a conflict, a draft, or an unreadable `ENABLE_AUTO_MERGE` never enables auto-merge.
- G4. `check_in_status.py` reports `wait` (never `review-round`) for a pending-checks head, and `ci-failed` in `--hand-back` mode once a check on that head fails.
- G5. The review gate skips dispatched re-runs on a pending-checks head (`claude_fixer_pending_checks`), so the reviewer panel does not repeat while CI runs.
- G6. A `changelog.d/` fragment (§20).

## Non-goals

- Changing `.claude/scripts/check_in_status.py` or any other `.claude/**` file (AD-3).
- A `check_suite` / `workflow_run` completion trigger (AD-1).
- Changing the findings, conflict, or verdict-convergence paths, the review-time failed-check hand-off, or the doc-only / small-diff deterministic skip.
- #4835 (stalled reviewer slot).

## Constraints

- §1 / §3: fail closed on every uncertain read; auto-merge stays head-bound.
- §5: extend the hand-off script, the gate's existing Claude-fixer block, and the catch-all sweep; one new helper module for the readiness half.
- §6: no renames. New identifiers (`ai:claude-fixer-pending-checks:v1`, `claude_fixer_pending_checks`, `gate_claude_pending_checks_on_head`, `scripts/claude_fixer_pending_checks.py`) were checked and are unused.
- §9: tabs in Python / shell bodies that already use them; YAML stays 2-space.
- §15: the readiness half is one paginated check-runs read per evaluation, through `collect_pr_check_runs_context.py` (as the issue asks), plus the reads listed in the new module's docstring.
- §18: no manual script; the readiness half runs inside the existing hourly `claude-pr-catch-all` job.
- §19: PR bodies use `Refs #4900`, the final PR `Fixes #4900`.
- §20: changelog fragment. §27: `review_autofix.yml` is 454,700 bytes; the gate change adds well under 2 KB (stays below 480,000).

## Approach

1. **Hand-off step.** After the existing "ready" test fails, a second test accepts the same snapshot header and head, `collection_status` `ready` or `timeout` (AD-8), `total_check_runs >= 1`, `failed_count: 0`, `incomplete_count >= 1`, and no `failed[...]` entry. It posts one comment headed `## Review round <n>: clean review, waiting for check runs` with the reviewed head's run link, the incomplete check names, the ledger digest, and the pending-checks marker, logs `CLAUDE_FIXER_HANDOFF ... kind=pending-checks`, and exits 0 (no `CLAUDE_FIXER_ZERO_FINDINGS`, no verification-failed flag, since the review was clean).
2. **Gate.** A predicate `gate_claude_pending_checks_on_head` (next to `gate_claude_handoff_on_head`, same trust rule: GH_PAT author, issued header, marker for the current head) extends the dispatch skip: `SKIP_REASON=claude_fixer_pending_checks`.
3. **Readiness half.** `scripts/claude_fixer_pending_checks.py` decides for one PR: finds the latest trusted pending-checks comment for the current head that no later trusted hand-off for that head supersedes, snapshots the head's check runs with `collect_pr_check_runs_context.py` (`CHECK_RUNS_WAIT_TIMEOUT_SECS=0`, no log tails), verifies the linked review run, reads the PR (draft, labels, conflict, `auto_merge`, head), reads the repo's `ENABLE_AUTO_MERGE` variable (AD-6), and runs `review_enable_auto_merge.sh`. `scripts/claude_pr_sweep.py` calls it for every candidate whose hand-back verdict is `open` (not claimed, held, or due).
4. **Checker.** No change: the pending-checks comment is not a hand-off, so `check_in_status.py` keeps waiting; a check that fails later reaches its existing `ci-failed` path (AD-3, AD-4). Tests lock this in.

Alternatives: a new job in `review_autofix.yml` fed by the 30-minute sweep dispatch covers only this repo and grows the 455 KB workflow; a `workflow_run` trigger needs each consumer's CI workflow names. Both rejected (AD-1).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — pending-checks marker, gate skip, and sweep auto-merge.** Files: see [Files & Modules](#files--modules). Done when the hand-off script posts the pending-checks comment for the #4869 snapshot, the gate skips dispatches on such a head, the sweep enables head-bound auto-merge once the snapshot is ready (and never otherwise), `check_in_status.py` reports `wait` / `ci-failed` for it, and the repo's CI passes. Rollback: revert the PR; a pending-checks comment left on an open PR is then inert (not a hand-off), and the next push reviews normally.

## Implementation Steps

Phase 1:
1. `scripts/review_autofix_step_claude_fixer_handoff.sh`: add the pending-checks branch after the ready test (lines ~140-153) and document it in the header comment.
2. `.github/workflows/review_autofix.yml` gate: add `gate_claude_pending_checks_on_head()` beside `gate_claude_handoff_on_head()` and extend the `claude_fixer_awaiting_session` branch with the `claude_fixer_pending_checks` skip.
3. `scripts/claude_fixer_pending_checks.py` [new]: marker parsing, snapshot, run verification, `ENABLE_AUTO_MERGE` read, merge call; docstring states the API budget and fail-closed rules.
4. `scripts/claude_pr_sweep.py`: call the evaluator for `open` verdicts (injectable for tests), count `pending_checks_merged` / `pending_checks_waiting`, honour `--dry-run`.
5. Tests (below) and the `ci.yml` step that runs `tests/test_claude_pr_sweep.py`.
6. `README.md`, `agents.md`, `docs/INVENTORY.md`: document the marker, the gate skip, and the sweep's new duty.
7. `changelog.d/4900-claude-fixer-pending-checks-auto-merge.md`.

## Files & Modules

- `scripts/review_autofix_step_claude_fixer_handoff.sh`
- `scripts/claude_fixer_pending_checks.py` [new]
- `scripts/claude_pr_sweep.py`
- `.github/workflows/review_autofix.yml`
- `.github/workflows/ci.yml`
- `tests/test_claude_fixer_pending_checks.py` [new]
- `tests/test_review_autofix_claude_fixer_mode.py`
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/4900-claude-fixer-pending-checks-auto-merge.md` [new]

## Tests

- Unit (hand-off script, stubbed `gh`): the #4869 snapshot (`timeout`, `incomplete_count: 1`) posts the pending-checks comment and no findings hand-off; `ready` + incomplete also pends; a failed entry, another head, `api_error`, `disabled`, `total_check_runs: 0`, or an unclean ledger does not pend.
- Unit (gate, real script with stubbed `gh`): a trusted pending-checks marker on the head skips a dispatch as `claude_fixer_pending_checks`; an untrusted or other-head marker does not.
- Unit (`check_in_status.py`, stubbed reads): a pending-checks head with running checks reports `wait` in plain and `--hand-back` modes (never `review-round`); after a check fails with no active run, `--hand-back` reports `ci-failed`.
- Unit (evaluator): ready → `review_enable_auto_merge.sh` invoked with the head; incomplete, failed, malformed snapshot, head moved, superseded or untrusted marker, unsuccessful review run, blocking label, conflict, draft, auto-merge already on, `ENABLE_AUTO_MERGE=false`, or an unreadable variable → no merge.
- End-to-end (#4869 sequence): run the hand-off script on the pending snapshot, feed its posted comment to the sweep with CI later green, assert one head-bound merge call and no findings hand-off; the same sequence with a failed check hands off as `ci-failed`.
- Existing suites: `tests/test_review_autofix_claude_fixer_mode.py`, `tests/test_claude_pr_sweep.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_workflow_file_size_limit.py`.

## Risks & Mitigations

- A PAT without Actions-variables read cannot read `ENABLE_AUTO_MERGE` in a consumer → the sweep logs a warning and does not merge (fail closed); the PR stays as today.
- Hourly cadence → a pending PR merges up to ~1 h after CI finishes. ACCEPTED — the issue allows "on a schedule"; still no human.
- A `cancelled` / `stale` check counts as failed in the collector but not in `check_in_status.py` → no merge and no fix hand-off (AD-9); logged by the sweep. ACCEPTED — the same outcome as today for that head; a push or re-run clears it.
- `/implement-plan-claude` heads keep the 6-hour stuck window for a later failed check (AD-4). ACCEPTED — existing checker behaviour.
- Consumers get the gate and hand-off change with the next `@stable`; the sweep runs from `main` and simply finds no markers until then.

## Rollout

Ships with the PR merge for this repo (the internal review uses `review_autofix.yml@main`, and the sweep runs from `main`), and for consumers with the next `@stable` release. Kill switches: `CLAUDE_FIXER_ENABLED=false` (whole mode) and `ENABLE_AUTO_MERGE=false` (per repo, honoured by the sweep). Rollback: revert.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Where does the readiness re-check run? — Picked: A — in the hourly `claude-pr-catch-all` sweep (`scripts/claude_pr_sweep.py`), which already covers this repo and every consumer with `GH_PAT`. Alternatives: B — a new `review_autofix.yml` job fed by the 30-minute review sweep dispatch (this repo only; grows a 455 KB workflow); C — a `workflow_run` completion trigger (needs each consumer's CI workflow names). Why: one place, all repos, no reviewer re-run, no workflow growth. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What does the hand-off step leave for a clean review with pending checks? — Picked: A — a workflow-owned comment with its own header and `<!-- ai:claude-fixer-pending-checks:v1 head=<sha> round=<n> ledger=<sha256> -->`. Alternatives: B — no comment, only a job output (nothing durable for the sweep or the gate to read). Why: the sweep and the gate need a trusted, head-bound record, and the comment tells humans why the PR is waiting. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Does `check_in_status.py` (a protected `.claude/**` path) change? — Picked: A — no; the new marker is not a hand-off, so it already routes to `wait`, and tests lock that in. Alternatives: B — add an explicit `pending-checks` state (a protected-path edit that would stop this unattended chain, §28.C). Why: the behaviour the issue asks for needs no edit there (§5). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-29] What happens when a pending check later fails? — Picked: A — nothing new is posted; `check_in_status.py --hand-back` reports `ci-failed` (as the issue's acceptance asks), and `/implement-plan-claude` heads keep their 6-hour stuck window. Alternatives: B — the sweep posts a `kind=findings` hand-off naming the failed check (reports `review-round` for a 0-entry ledger, which the issue rules out). Why: matches acceptance. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Should dispatched re-runs skip a pending-checks head? — Picked: A — yes, like `claude_fixer_awaiting_session`, with skip reason `claude_fixer_pending_checks`. Alternatives: B — let the 30-minute review sweep re-run the reviewer panel (wasteful; the issue says no reviewer re-run). Why: cost and intent. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] How does the sweep honour a repo's auto-merge policy? — Picked: A — read that repo's `ENABLE_AUTO_MERGE` variable (404 = unset = `true`, any other failure = do not merge) and run the existing `review_enable_auto_merge.sh` with it, keeping its head-bound, e2e-label, and integration-branch guards. Alternatives: B — call `gh pr merge --auto` directly (drops those guards and the repo switch). Why: one auto-merge policy. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Does the sweep also run "Mark linked issues ready to merge"? — Picked: A — no; it only enables auto-merge. Alternatives: B — port the label step (its linked-issue inputs exist only inside the review run). Why: §5; `claude/*` PRs link issues with `Refs`, and the issue asks for the merge. Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-29] Does `collection_status: timeout` count as "pending"? — Picked: A — yes, with `failed_count: 0` and `incomplete_count > 0`; `ready` with incomplete runs too. Alternatives: B — `ready` only (the collector's 300 s wait ends in `timeout` whenever CI is long, which is the #4869 case). Why: that is the case the issue describes. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-29] A `cancelled` / `stale` check (failed for the collector, not for `check_in_status.py`)? — Picked: A — never merge; log it and leave the PR to the existing paths. Alternatives: B — treat it as green (unsafe). Why: fail closed (§1). Applied in: phase 1 PR. Status: pending review

## References

- Issue #4900; PR #4869 and run 36501676228; #4835; #4707.
- `docs/plans/claude-fixer-unattended-convergence-plan.md` (the verdict-convergence design this leaves unchanged).
