# Recognize consumer `ai-review.yml` PR-named dispatch runs in the §26 checker

Source issue: shubhodeep1/coding-workflows#4926 (https://github.com/shubhodeep1/coding-workflows/issues/4926)
Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

Security finding `consumer-review-handoff-unrecognized` (medium, STRIDE: Denial of Service) at `.claude/scripts/check_in_status.py:333`. Issue #4701's project moved the poller's and merge train's review dispatches to the default branch, and the consumer wrapper `workflow-templates/ai-review.yml` now names its dispatched runs `AI Review [pr:<N>]`. The checker still recognizes only `internal-review.yml` runs titled `Internal: AI Review & Autofix [pr:<N>]`. So in a consumer repo, a Claude-fixer hand-off whose review run is a default-branch `ai-review.yml` dispatch is never accepted: `check_in_status.py --hand-back` (the §26 checker and the §26.H catch-all sweep, `scripts/claude_pr_sweep.py`) keeps the PR at `open` / `waiting for verified completed review run`, and no fixer is queued. The active-run count has the same blind spot, so a running consumer dispatch does not hold a hand-off back.

The fix recognizes both approved wrapper paths, each with its exact PR-scoped run name, in the hand-off verification and in the active-run count.

## Context

- `.claude/scripts/check_in_status.py` on the base branch (#4618's fix, carried into #4701's project):
  - `DISPATCHED_REVIEW_WORKFLOW = ".github/workflows/internal-review.yml"`, `DISPATCHED_REVIEW_TITLE = "Internal: AI Review & Autofix [pr:{number}]"`, `DISPATCHED_REVIEW_RUNS_PATH` = the `internal-review.yml` `workflow_dispatch` run listing (lines 134–136).
  - `_is_pr_dispatched_review_run` (line 321) accepts a hand-off's review run only when its `path` is `internal-review.yml` and its `display_title` is the internal title. A consumer run (`path` `.github/workflows/ai-review.yml`, title `AI Review [pr:<N>]`, `head_branch` = default branch) fails it and then fails the head-branch check, so `_check_claude_fixer_pr` returns `waiting for verified completed review run` forever.
  - `_active_run_count` (line 282) reads only `internal-review.yml`'s dispatch runs and treats HTTP 404 as zero, so in a consumer repo it never sees an active `ai-review.yml` dispatch.
- Run names: `.github/workflows/internal-review.yml:8` and `workflow-templates/ai-review.yml:12`. A `workflow_dispatch` run's name is evaluated from the dispatched ref's workflow file, which is the default branch at every dispatch site after #4618 and #4701, so the exact title binds the run to its PR.
- The poller already matches both names (`_pr_named_review_dispatch_runs`, `scripts/orchestrate_poll_process.sh`), with one repo-wide `workflow_dispatch` listing (#4701's AD-4).
- `workflow-templates/.claude/scripts/check_in_status.py` is the byte-identical copy consumer repos receive; both copies change together.
- Callers: `check_pr` (project checker, `--pr N`), `check_pr_hand_back` (§26 checker, `--hand-back`), `scripts/claude_pr_sweep.py` (catch-all, imports `check_pr_hand_back`).

## Goals

- `_is_pr_dispatched_review_run` accepts an `ai-review.yml` `workflow_dispatch` run on the default branch titled `AI Review [pr:<N>]`, as it accepts the `internal-review.yml` pair today.
- A wrapper path and a title are accepted only as their own pair: an `ai-review.yml` run with the internal title, or the reverse, is rejected.
- `_active_run_count` counts an active PR-named dispatch run of either wrapper.
- Both copies of `check_in_status.py` stay identical.

## Non-goals

- No change to the run names, the dispatch sites, the poller, the merge train, or `review_autofix.yml`.
- No change to hand-off parsing, claims, caps, or routing.
- `review_autofix.yml` dispatched directly (the last candidate in the poller and merge train) has no PR run name and stays unrecognized as a default-branch dispatch, as #4701's AD-10 decided.

## Constraints

- §6: `DISPATCHED_REVIEW_WORKFLOW`, `DISPATCHED_REVIEW_TITLE`, and `DISPATCHED_REVIEW_RUNS_PATH` stay defined with their current values; new identifiers are added beside them and checked for collisions.
- §15: the active-run count keeps at most one extra read, issued only when the head-branch reads found nothing (AD-2).
- §5: only the two functions, their constants, and their docstrings change.
- §9: tabs in Python.
- §20: a `changelog.d/` fragment (consumer-visible behaviour).
- §28.C: the phase edits `.claude/scripts/check_in_status.py`, a protected path, so it starts only under a recorded `Protected-path approval:` line.

## Approach

- Add `PR_NAMED_REVIEW_DISPATCHES`, a tuple of `(workflow path, title template)` pairs: `(".github/workflows/internal-review.yml", "Internal: AI Review & Autofix [pr:{number}]")` (from the existing constants) and `(".github/workflows/ai-review.yml", "AI Review [pr:{number}]")`.
- `_is_pr_dispatched_review_run`: accept the run when `(path without @ref, display_title)` equals one pair formatted for the PR, together with the existing `event == workflow_dispatch`, `head_branch == default_branch`, and known-default-branch conditions.
- `_active_run_count`: when the head-branch reads found nothing and a PR number is given, read one repo-wide listing, `repos/{repo}/actions/runs?event=workflow_dispatch&per_page=100` (new constant `PR_NAMED_REVIEW_RUNS_PATH`), and count runs with an active status whose `(path, display_title)` is one of the pairs for the PR. A failed read raises `ReadError` as today (the repo-wide listing has no per-workflow 404 case; AD-3).
- Docstrings state the pairs, the one-call budget, and the 100-run window.

## Phases & Merge Strategy

This is a single-phase plan: issue mode (CLAUDE.md §28.A) authorises it, because `/implement-issue-claude` always turns one issue into one phase.

1. **Phase 1 — recognize both PR-named review wrappers in `check_in_status.py`.** protected paths: `.claude/scripts/check_in_status.py`
   - Files: `.claude/scripts/check_in_status.py`, `workflow-templates/.claude/scripts/check_in_status.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_check_in_status.py`, `changelog.d/4926-consumer-review-handoff.md`.
   - Done when:
     - a consumer `ai-review.yml` default-branch dispatch hand-off is `review-round` / `conflict` in `--hand-back` and `--pr` modes;
     - mismatched pairs, another PR's title, and a non-dispatch event still fail closed;
     - an active `AI Review [pr:<N>]` run keeps a hand-off waiting;
     - both script copies are identical (`cmp`), and the changed test modules plus `tests/test_claude_pr_sweep.py` pass.
   - Rollback: revert the phase PR; the checker returns to recognizing `internal-review.yml` dispatches only.

## Implementation Steps

1. `.claude/scripts/check_in_status.py`: add `PR_NAMED_REVIEW_DISPATCHES` and `PR_NAMED_REVIEW_RUNS_PATH` after the existing `DISPATCHED_REVIEW_*` constants, with a comment naming both wrappers and keeping the old constants for §6.
2. Rewrite the match in `_is_pr_dispatched_review_run` to use the pairs; update its docstring.
3. Rewrite the PR-named branch of `_active_run_count` to read `PR_NAMED_REVIEW_RUNS_PATH` once and match the pairs; update its docstring (one call, raise on failure).
4. Copy the file to `workflow-templates/.claude/scripts/check_in_status.py`.
5. Tests and the changelog fragment (below).

## Files & Modules

- `.claude/scripts/check_in_status.py`
- `workflow-templates/.claude/scripts/check_in_status.py`
- `tests/test_check_in_status_hand_back.py`
- `tests/test_check_in_status.py`
- `changelog.d/4926-consumer-review-handoff.md` [new]

## Tests

- `tests/test_check_in_status_hand_back.py`:
  - switch `DISPATCH_RUNS` to the repo-wide listing path; existing internal-review tests keep passing;
  - a findings and a conflict hand-off whose review run is an `ai-review.yml` default-branch dispatch titled `AI Review [pr:7]` are due;
  - fail-closed cases: `ai-review.yml` with the internal title, `internal-review.yml` with `AI Review [pr:7]`, `AI Review [pr:8]`, a trailing space, `event: pull_request`;
  - an active `AI Review [pr:7]` run (each of queued / in_progress / pending) keeps the hand-off waiting; one on another wrapper path or for another PR does not count;
  - replace the 404 test: a failed repo-wide read raises `ReadError`; a head-branch active run still skips the listing.
- `tests/test_check_in_status.py`: switch its `DISPATCH_RUNS` path and add one `--pr` stuck-path case with an active `AI Review [pr:N]` run.
- `tests/test_claude_pr_sweep.py`: run unchanged.
- Local verification: the three test modules, `cmp` of the two script copies, `python3 -m py_compile`.

## Risks & Mitigations

- The repo-wide listing mixes every workflow's dispatch runs, so an active review run can fall off the newest-100 page in a busy repo. ACCEPTED — the same window #4701's AD-4 accepted for the poller (about 4 hours in this repo, where most dispatch runs are `internal-review.yml`); a miss means a hand-off is acted on while a review run is still active, which the claim lease and the fixer's stale-head check already absorb.
- A consumer repo still on the pre-#4701 `ai-review.yml` has no run name: nothing matches, which is today's behaviour.
- The base branch (#4701's project, final PR #4709 into #4618's project branch) merges while this project is in flight. Mitigation: the issue-mode base-move rule retargets the final PR.

## Rollout

No flag. This repo gets the change when the final PR reaches `main` through #4701's and #4618's projects; consumer repos get the `workflow-templates/.claude/` copy on the next `@stable` sync. Roll back by reverting the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-29] Which base branch does the project build on? — Picked: A — the issue's `Integration branch:` `claude/implement-plan-issue-4701-review-dispatch-default-branch`. Alternatives: B — `main`. Why: the issue names it, and the consumer `run-name` and the #4618 checker code exist only on that branch. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-29] How does `_active_run_count` find PR-named runs of both wrappers? — Picked: A — one repo-wide `workflow_dispatch` listing matched on both (path, title) pairs. Alternatives: B — one per-wrapper listing each, with 404 counted as zero (two calls in every repo, one always a 404); C — `internal-review.yml` first, then `ai-review.yml` on 404 (two calls in every consumer repo). Why: one call in both repo kinds (§15), and the same listing and window the poller uses since #4701's AD-4. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does the repo-wide listing fail? — Picked: A — any failed read raises `ReadError` (exit 2, `action: retry`), as every other read does. Alternatives: B — fail open to zero. Why: the per-workflow 404 case no longer exists, and failing open would let a hand-off start a fixer while a review may be running. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Must the wrapper path and the title match as a pair? — Picked: A — yes, each wrapper only with its own title. Alternatives: B — accept either title from either wrapper path. Why: pairing is the tighter binding and costs nothing; each wrapper's `run-name` produces only its own title. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Keep the old `DISPATCHED_REVIEW_*` constants? — Picked: A — keep all three with their values and add the new ones beside them. Alternatives: B — replace them. Why: §6 naming immutability. Applied in: phase 1 PR. Status: pending review

## Notes

- Security pass: `security_pass_skip.py` returned `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The phase edits `.claude/scripts/check_in_status.py` (protected path, CLAUDE.md §28.C). Project #4618 wrote its `check_in_status.py` fixes in a watched session, then twin-first (edit only the `workflow-templates/` copy, the supervising session syncs `.claude/`), while issue #4785 (land `.claude/` changes without a watched session) is open.

## References

- Issue #4926; issue #4701 and its project (final PR #4709); issue #4618 and its project (final PR #4634).
- `scripts/orchestrate_poll_process.sh` `_pr_named_review_dispatch_runs`.
- Issue #4785.
