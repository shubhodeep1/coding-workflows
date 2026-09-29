# Pending-checks auto-merge: defer while a newer review of the PR is active

Source issue: shubhodeep1/coding-workflows#5148 (https://github.com/shubhodeep1/coding-workflows/issues/5148)
Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
Security pass: skip (ai:security: automation-produced issue)

## Summary

The `claude-pr-catch-all` sweep's pending-checks pass (issue #4900) enables auto-merge from an earlier clean review's marker even while a newer review of the same PR (a forced or judge dispatch from the default branch, or a re-run) is still running. This plan makes the pass defer while any newer review of the PR is queued or running, refuse to authorize when the latest newer completed review of the PR did not succeed, and re-confirm the marker right before the merge call.

## Context

- Security audit finding `pending-merge-races-newer-review` (high, confidence 8/10, `A01:2021-Broken Access Control`) at `scripts/claude_pr_sweep.py:253`, filed by `.github/workflows/security-audit.yml` against the #4900 project branch (tracker #3576).
- `scripts/claude_pr_sweep.py:253` calls `pending_checks(repo, number, dry_run)` = `claude_fixer_pending_checks.evaluate` for every `open` candidate.
- `scripts/claude_fixer_pending_checks.py` `evaluate()` reads the PR, finds the live marker (`find_pending_marker`), snapshots the head's check runs, verifies only the **marker's own** review run (`verify_review_run`), reads `ENABLE_AUTO_MERGE`, and runs `scripts/review_enable_auto_merge.sh`. It never looks at other review runs of the PR.
- A newer review can run on the same head while the head's checks are green:
  - `.github/workflows/review_autofix.yml` gate (lines ~763-843): a `workflow_dispatch` on a pending-checks head is skipped (`claude_fixer_pending_checks`), **except** `force_rb_judge` dispatches and the `force-review` label / `[force-review]` title, which run the full reviewer panel.
  - `.github/workflows/review_autofix_sweep.yml` dispatches `internal-review.yml` from the default branch every 30 minutes (issue #4618), titled `Internal: AI Review & Autofix [pr:<N>]`. Its check runs attach to the default-branch commit, so the PR head's snapshot stays `ready` while it runs.
  - `review_autofix.yml` (this repo) and consumer `ai-review.yml` accept direct `workflow_dispatch` from the default branch with no PR in the run name.
  - Re-running the marker's run keeps its id, so `verify_review_run` sees `in_progress` and already refuses; nothing else covers the cases above.
- Nothing disables auto-merge when a later review posts findings (no `--disable-auto` anywhere in `scripts/` or `review_autofix.yml`), and with green required checks GitHub merges within seconds. The race therefore has to be closed before the merge call.
- `.claude/scripts/check_in_status.py` already defines the pieces to bind a run to a PR: `FIXER_WORKFLOW_PATHS`, `DISPATCHED_REVIEW_TITLE`, and `DISPATCHED_REVIEW_RUNS_PATH`.

## Goals

- G1. `evaluate()` returns `review_active` and does not merge (dry run included) while any of these is queued, running, or otherwise not `completed`: a workflow run on the PR's head branch; an `internal-review.yml` `workflow_dispatch` run titled for this PR; any `workflow_dispatch` run of `review_autofix.yml` or `ai-review.yml` (unbound to a PR, so fail closed).
- G2. `evaluate()` returns `review_superseded` and does not merge when the latest completed review run bound to this PR that is newer than the marker's run (a head-branch run of a review workflow, or an `internal-review.yml` dispatch for this PR) did not conclude `success`.
- G3. Right before the merge decision, `evaluate()` re-reads the PR's comments and returns `review_superseded` unless the same marker comment is still the live one (closes the window where a newer review finishes between the first comment read and the run reads).
- G4. Every read failure keeps raising `check_in_status.ReadError`, which the sweep already logs per PR as `pending_checks_failed`; a 404 on a review workflow's dispatch listing (the workflow does not exist in that repo) counts as no runs.
- G5. Tests reproduce the audit's exploit scenario (a forced review dispatched from the default branch still running while the old head's checks are green) and every other defer / supersede path.
- G6. A `changelog.d/` fragment (§20, security fix) and doc updates in `README.md`, `agents.md`, `docs/INVENTORY.md` (§7).

## Non-goals

- Any `.claude/**` change, including `.claude/scripts/check_in_status.py` (protected path; its constants are reused read-only).
- Disabling auto-merge after it was enabled, or changing the review workflow's own auto-merge path, the gate, or the hand-off step.
- Binding consumer `ai-review.yml` / `review_autofix.yml` dispatches to a PR by run name (they have none; AD-1 fails closed instead).
- New sweep counters (AD-5).

## Constraints

- §1 / §3: fail closed on every uncertain read or unbindable run.
- §5: extend `scripts/claude_fixer_pending_checks.py` only; `scripts/claude_pr_sweep.py` needs no code change (its per-PR `except Exception` already covers a new `ReadError`).
- §6: no renames. New identifiers `review_active`, `review_superseded`, `check_review_runs`, `_read_review_run_listing`, `_run_path`, `UNBOUND_DISPATCH_REVIEW_WORKFLOWS`, `REVIEW_RUNS_PER_PAGE` were checked with a repo-wide grep and are unused.
- §9: tabs in the Python module and tests.
- §15: the new reads run only for a PR whose marker is live, whose snapshot is `ready`, and whose own review run is verified: 1 head-branch runs listing, 1 dispatch listing per review workflow (`internal-review.yml`, `review_autofix.yml`, `ai-review.yml`; at most 3, a 404 costs its one call), and 1 re-read of the comments (1 call per 100). The module docstring states the budget.
- §18: no new script; runs inside the existing hourly `claude-pr-catch-all` job.
- §19: every PR uses `Refs #5148`; the final PR targets the #4900 project branch, so it uses `Refs #5148` and the final-merge stage closes the issue explicitly.
- §20: a `security` fragment.

## Approach

Add `check_review_runs(repo, number, head_ref, marker_run_id, default_branch)` to `scripts/claude_fixer_pending_checks.py`. It returns `("active" | "superseded", reason)` or `None`:

1. Read `repos/<repo>/actions/runs?branch=<head_ref>&per_page=100`. Any run whose `status` is not `completed` → active. Completed runs whose workflow path is in `FIXER_WORKFLOW_PATHS` and whose id is greater than the marker's run id are candidates for "newer completed review".
2. For each of `internal-review.yml`, `review_autofix.yml`, `ai-review.yml`: read `repos/<repo>/actions/workflows/<file>/runs?event=workflow_dispatch&per_page=100` (404 → skip). `internal-review.yml` runs are bound by `DISPATCHED_REVIEW_TITLE` for this PR (the title alone, as `check_in_status._active_run_count` does; a broader match can only block a merge): not completed → active; completed and newer than the marker's run → candidate. `review_autofix.yml` / `ai-review.yml` dispatch runs carry no PR binding: any not completed → active; completed ones are ignored.
3. If no run is active, the candidate with the highest id must have conclusion `success`, else superseded.

`evaluate()` calls it after `verify_review_run` succeeds, then re-reads the comments and requires `find_pending_marker` to return the same `comment_id`, and only then reads `ENABLE_AUTO_MERGE` and merges. Gate-skipped dispatch runs conclude `success`, so the 30-minute sweep dispatch never blocks a merge; a newer review that posted findings supersedes the marker through `find_pending_marker` (existing rule); a newer clean review posts its own marker.

Alternatives: calling `check_in_status._active_run_count` (3–4 reads, `queued` / `in_progress` / `pending` only, no completed runs) plus separate completed-run reads costs more calls and misses other non-completed statuses (AD-4); requiring the marker's run to be the latest completed review would block every PR behind the gate-skipped sweep dispatches (AD-2).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — defer the pending-checks merge while a newer review is active or unsettled.** Files: see [Files & Modules](#files--modules). Done when `evaluate()` returns `review_active` / `review_superseded` without a merge call for every case in [Tests](#tests), the existing #4900 tests still pass, the docs and changelog fragment are updated, and the repo's CI passes. Rollback: revert the PR; the pass then behaves as before this plan (the race returns, nothing else changes).

## Implementation Steps

Phase 1:
1. `scripts/claude_fixer_pending_checks.py`: add `UNBOUND_DISPATCH_REVIEW_WORKFLOWS`, `REVIEW_RUNS_PER_PAGE`, and `check_review_runs()`; call it from `evaluate()` after `verify_review_run`, followed by the comment re-read; add the two states to `evaluate()`'s docstring and the module docstring (fail-closed list, API budget).
2. `tests/test_claude_fixer_pending_checks.py`: extend the stub `gh` with state-driven head-branch and dispatch run listings (status filters honoured, so `check_in_status.py` reads keep working); add the tests below.
3. `README.md`, `agents.md`, `docs/INVENTORY.md`: one sentence each on the new deferral.
4. `changelog.d/5148-pending-checks-newer-review-race.md` (`<!-- changelog: security -->`).

## Files & Modules

- `scripts/claude_fixer_pending_checks.py`
- `tests/test_claude_fixer_pending_checks.py`
- `README.md`, `agents.md`, `docs/INVENTORY.md`
- `changelog.d/5148-pending-checks-newer-review-race.md` [new]

## Tests

- Exploit scenario: marker on the head, checks green, an `internal-review.yml` dispatch titled `[pr:42]` from the default branch `in_progress` → `review_active`, no merge call.
- Queued / in-progress / pending (and `waiting`) run on the head branch → `review_active`.
- Active `review_autofix.yml` or `ai-review.yml` dispatch (unbound) → `review_active`; an active `internal-review.yml` dispatch for another PR → merges.
- Newer completed bound review run (head-branch review workflow, or `internal-review.yml` dispatch for this PR) with conclusion `failure` / `cancelled` → `review_superseded`; the same with `success` → merges; an older failed run (id below the marker's) → merges; a newer completed non-review run on the head branch → merges.
- A 404 on a dispatch listing counts as no runs; any other read failure raises `ReadError` (the sweep logs `pending_checks_failed`).
- Marker changed between the two comment reads (a newer hand-off or a newer marker) → `review_superseded`.
- Dry run with an active review → `review_active` (not `ready`).
- Existing suites: `tests/test_claude_fixer_pending_checks.py`, `tests/test_claude_pr_sweep.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_review_autofix_claude_fixer_mode.py`.

## Risks & Mitigations

- An active unbound dispatch in the repo (for any PR) defers every pending-checks merge by one sweep hour. ACCEPTED — fail closed; these dispatches are rare and short.
- A head branch with more than 100 runs could hide an old active run from the 100-run listing. ACCEPTED — a claude/* branch with 100+ runs is implausible, and such a run would be far older than the review.
- A review that starts after the re-read and before the merge call cannot be seen. ACCEPTED — a window of seconds, while a review takes minutes to post anything.
- More reads per ready PR (≤ 5 plus pagination). ACCEPTED — only for PRs about to merge, a handful per hour.

## Rollout

Ships when the #4900 project's final PR merges into `main` (the sweep runs from `main`); nothing to configure. Kill switches unchanged: `CLAUDE_FIXER_ENABLED=false` and `ENABLE_AUTO_MERGE=false`. Rollback: revert.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which review runs count as a newer review of the PR? — Picked: A — every run on the PR's head branch, `internal-review.yml` dispatches bound by the `[pr:<N>]` title, and (fail closed) any active `review_autofix.yml` / `ai-review.yml` dispatch, which carry no PR binding. Alternatives: B — title-bound dispatches only (misses `force_rb_judge` and consumer forced dispatches); C — any active workflow run in the repository (stalls every merge behind unrelated work). Why: covers every path the gate lets a forced review take, at the cost of an occasional one-hour delay. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What does "authorize only against the latest completed review" mean? — Picked: A — the latest completed review bound to this PR that is newer than the marker's run must have concluded `success`, and the same marker must still be live on a re-read of the comments right before the merge. Alternatives: B — the marker's run must be the latest completed review (every gate-skipped 30-minute sweep dispatch would block the merge forever); C — active runs only (a crashed forced review would not block). Why: a successful newer run either skipped or left its own comment, which the re-read sees; a failed one left no verdict. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which statuses count as active? — Picked: A — any status other than `completed` (queued, in_progress, pending, waiting, requested). Alternatives: B — `queued` / `in_progress` / `pending` like `check_in_status._active_run_count`. Why: fail closed (§1). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Reuse `check_in_status._active_run_count` or read the listings directly? — Picked: A — one head-branch listing and one dispatch listing per review workflow, reusing `check_in_status`'s constants. Alternatives: B — call `_active_run_count` and add completed-run reads (more calls, active statuses limited to three). Why: fewer calls (§15), one read answers both questions. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Do the new states change the sweep's counters? — Picked: A — no; they are logged by the existing `pending_checks` line. Alternatives: B — count `review_active` as `pending_checks_waiting`. Why: §5, and the counter means "checks still running". Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-29] Where does the fix live? — Picked: A — in `evaluate()` (`scripts/claude_fixer_pending_checks.py`), which the flagged line calls. Alternatives: B — in the sweep loop in `scripts/claude_pr_sweep.py`. Why: the module owns the merge decision and its tests; the sweep's per-PR error handling already covers it. Applied in: phase 1 PR. Status: pending review

## References

- Issue #5148; security tracker #3576; issue #4900 and its plan `docs/plans/issue-4900-claude-fixer-pending-checks-auto-merge-plan.md`; issue #4618 (default-branch sweep dispatches).
