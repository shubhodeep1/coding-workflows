# Page through the review wrappers' active dispatch runs in the §26 checker, and defer a hand-back when the listing is incomplete

Source issue: shubhodeep1/coding-workflows#5442 (https://github.com/shubhodeep1/coding-workflows/issues/5442)
Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
Security pass: skip (ai:security: automation-produced issue)

## Summary

`_active_run_count` in `.claude/scripts/check_in_status.py` finds the review runs dispatched from the default branch for a PR by reading one page of the repository's newest 100 `workflow_dispatch` runs (`PR_NAMED_REVIEW_RUNS_PATH`, line 148). After 100 newer unrelated dispatches, a live review falls off that page, the checker reports no active run, and a Claude fixer is handed a PR whose review is still running (security finding `handback-misses-active-review-beyond-first-page`, medium). This plan reads only the two review wrappers' own active dispatch runs, page by page, and turns an incomplete listing into a read failure, so the hand-back is deferred and retried.

## Context

- Issue #5442 was filed by `.github/workflows/security-audit.yml` (tracker #3576) against `.claude/scripts/check_in_status.py:148` on `claude/implement-plan-issue-4701-review-dispatch-default-branch`. Its `Integration branch:` line names that branch, so this project is built on it (its final PR #4709 into `main` is still open).
- Issue #4926 (PR #5021, merged into that branch) replaced the `internal-review.yml`-only listing with one repo-wide listing, `repos/{repo}/actions/runs?event=workflow_dispatch&per_page=100`, matched on two (workflow path, run name) pairs in `PR_NAMED_REVIEW_DISPATCHES`: `internal-review.yml` / `Internal: AI Review & Autofix [pr:<N>]` and `ai-review.yml` / `AI Review [pr:<N>]`. A failed read raises `ReadError` (exit 2, `action: retry`).
- `_active_run_count` is read only after the head-branch reads (`actions/runs?branch=<head>&status=<s>&per_page=1`) count nothing. Its callers are `check_pr`'s stuck path (`--pr`, statuses `queued`, `in_progress`), `_check_claude_fixer_pr` (hand-off and conflict, plus `pending`), and `check_pr_hand_back` (`--hand-back`, plus `pending`). `scripts/claude_pr_sweep.py` calls `check_pr_hand_back` directly and skips a PR on `ReadError`.
- Issue #4927 fixed the same crowding in the poller's `_pr_named_review_dispatch_runs` (`scripts/orchestrate_poll_process.sh`) with per-wrapper listings that page until `total_count` and report an incomplete listing to their callers. Its log (`docs/implement-plan/issue-4927-paginate-review-dispatch-runs.md`, AD-16) recorded that this checker kept the single-page exposure.
- Measured 2026-09-29: `internal-review.yml` alone started 701 `workflow_dispatch` runs in 24 hours here, so the newest-100 page covers only a few hours. `GET actions/workflows/<wrapper>/runs?event=workflow_dispatch&status=<s>` returns `total_count` (verified 2026-09-30); a wrapper the repo does not have answers `gh: Not Found (HTTP 404)`.

## Goals

- `_active_run_count` counts PR-named review runs from the `workflow_dispatch` runs of `internal-review.yml` and `ai-review.yml` only, filtered server-side to each active status it already counts (`queued`, `in_progress`, and `pending` where included). Unrelated dispatches can no longer push a live review out of view.
- Each listing is read 100 runs at a time until every run its `total_count` reports has been read, at most `MAX_PAGINATED_API_PAGES` (10) pages.
- A wrapper whose first read answers HTTP 404 is absent from the repo, so its listing is complete and empty.
- Any other incomplete listing raises `ReadError`: a failed page, a malformed page, a missing `total_count`, more runs than 10 pages hold, or fewer distinct run ids read than `total_count` reports. The checker then reports `action: retry` and hands nothing back; the sweep skips the PR. The next check-in retries.
- On a complete listing the verdicts are unchanged: an active run of either wrapper with its own `[pr:<N>]` run name holds the hand-off back, and nothing else does.

## Non-goals

- The head-branch reads (`per_page=1`, `total_count`). They are scoped to the PR's own branch, so other dispatches cannot crowd them.
- Other run states (`requested`, `waiting`), which the current code does not count either.
- The `.claude/scripts/check_in_status.py` copy itself: under the interim twin-first default (CLAUDE.md §28.C, until #4785) the phase edits only the `workflow-templates/.claude/` twin, and the supervising session copies it as a `[claude-twin-sync]` commit.
- The poller, the merge train, and the sweep workflow's run snapshots.

## Constraints

- §5: only `_active_run_count`, one new helper and one new constant, the module and function docstrings, the two check-in test modules, and the docs that describe the listing.
- §6: no identifier is renamed or removed. `DISPATCHED_REVIEW_RUNS_PATH` and `PR_NAMED_REVIEW_RUNS_PATH` stay defined with their values (their comment says they are kept for compatibility); `_active_run_count` keeps its signature and return type. The new names (`PR_NAMED_REVIEW_WORKFLOW_RUNS_PATH`, `_pr_named_active_review_run_count`) are checked for collisions.
- §15: REST only. The PR-named branch goes from 1 call to one call per wrapper and status plus one per extra page, with a 404 short-circuit for an absent wrapper: 4 calls in `--hand-back` mode in this repo and in a consumer repo (3 statuses of the present wrapper, 1 404), 3 in `--pr` stuck mode. It is still issued only after the head-branch reads found nothing. The module docstring's API budget and the new helper's docstring state the input, output, calls, and failure behaviour.
- §8: an incomplete listing is a `ReadError` whose message names the wrapper, status, and reason, and it reaches the checker's JSON `error` field.
- §9: tabs, as the file uses.
- §20: one `changelog.d/` fragment (security).
- §28.C: `.claude/**` is a protected path; this phase runs twin-first.

## Approach

Add `PR_NAMED_REVIEW_WORKFLOW_RUNS_PATH = "repos/{repo}/actions/workflows/{workflow}/runs?event=workflow_dispatch&status={status}"` and a helper `_pr_named_active_review_run_count(repo, number, statuses)`. For each `(workflow path, title)` in `PR_NAMED_REVIEW_DISPATCHES` and each status, it reads the listing through `_gh_api_paginated_object(path, "workflow_runs")` (100 per page, 10-page cap, `ReadError` on a failed or malformed page). A 404 on the wrapper's first read ends that wrapper as absent; a 404 on any later read raises. After each listing, a missing `total_count`, or fewer distinct run ids than `total_count`, raises `ReadError` (the listing shifted under the pages). It counts runs whose status is the one requested and whose `(path, display_title)` passes `_is_pr_named_review_pair`. `_active_run_count` calls it in place of the single-page read.

Alternatives considered (AD-2): a per-wrapper listing windowed by `created` (the poller's shape) needs a time window the checker does not have and still pages through finished runs; a repo-wide listing filtered by status is 1 call fewer but can still be crowded by other workflows' active dispatches. Counting an incomplete listing as "active" instead of raising (AD-3) would defer silently forever; `retry` stops renewing the §26 dead-man's switch and, in the project checker, routes to a block stage after three retries, so a listing that stays incomplete is seen.

## Phases & Merge Strategy

Single phase. Issue mode (CLAUDE.md §28.A) authorises a single-phase plan: `/implement-issue-claude` always writes one phase per issue.

1. **Phase 1 — per-wrapper, paginated, completeness-checked active dispatch count** — protected paths: `.claude/scripts/check_in_status.py` (twin-first: edit `workflow-templates/.claude/scripts/check_in_status.py`).
   - Files: `workflow-templates/.claude/scripts/check_in_status.py`, `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`, `agents.md`, `CLAUDE.md`, `changelog.d/5442-page-checker-review-dispatch-runs.md`.
   - Done when: the twin reads per-wrapper, per-status listings page by page, treats a first-read 404 as absent, and raises `ReadError` on every incomplete case; the updated tests pass against the twin (and against `.claude/` once the twin sync lands); `python3 -m py_compile` is clean.
   - Rollback: revert the phase PR (and the twin-sync commit with it). The checker goes back to the single-page listing.

## Implementation Steps

1. `workflow-templates/.claude/scripts/check_in_status.py`: add `PR_NAMED_REVIEW_WORKFLOW_RUNS_PATH` after `PR_NAMED_REVIEW_RUNS_PATH` and reword the comment above the old constant (kept for §6, no longer read; it no longer claims the poller's window).
2. Same file: add `_pr_named_active_review_run_count` beside `_is_pr_named_review_pair`, and make `_active_run_count` call it; update `_active_run_count`'s docstring and the module docstring's API budget.
3. Tests (below).
4. `agents.md`: the verdict-helper bullet (the "one repo-wide `workflow_dispatch` listing (newest 100 runs)" sentence) and the §26 paragraph's "at most six further reads"; `CLAUDE.md` §26.C step 1's "at most six further reads", which the wrapper listings exceed.
5. `changelog.d/5442-page-checker-review-dispatch-runs.md`.

## Files & Modules

- `workflow-templates/.claude/scripts/check_in_status.py`
- `.claude/scripts/check_in_status.py` (only through the `[claude-twin-sync]` copy, never edited by the phase session)
- `tests/test_check_in_status.py`
- `tests/test_check_in_status_hand_back.py`
- `agents.md`
- `CLAUDE.md`
- `changelog.d/5442-page-checker-review-dispatch-runs.md` [new]

## Tests

- Unit (`tests/test_check_in_status_hand_back.py`, `tests/test_check_in_status.py`, `gh_api` stubbed by exact path):
  - the fixtures serve one listing per wrapper and status (`actions/workflows/<wrapper>/runs?event=workflow_dispatch&status=<s>&per_page=100&page=1`), and the existing call-sequence assertions list those reads in place of the repo-wide one;
  - an active matching run on page 2 of a wrapper listing (`total_count` 101) holds the hand-off back (the finding's scenario);
  - a wrapper whose first read is a 404 is skipped for its remaining statuses, and the other wrapper's active run still counts;
  - a non-404 failure, a 404 after the wrapper's first read, a malformed page, a missing `total_count`, a short listing (fewer distinct ids than `total_count`), and a listing past the page cap each raise `ReadError`, and `main` exits 2 with `action: retry`;
  - runs of another PR, of the other wrapper's run name, or with a status other than the one requested do not count;
  - no repo-wide `actions/runs?event=workflow_dispatch` read is issued.
- The existing `test_check_in_status*.py` parity tests stay red until the `[claude-twin-sync]` copy, then pass. The modules load `.claude/scripts/check_in_status.py`, so the updated tests pass once the copy lands; before it, they are run against the twin by pointing the loader at it in a scratch copy.
- `tests/test_claude_pr_sweep.py` runs unchanged (it catches `ReadError` from `check_pr_hand_back`).
- The CI steps that already run these files (`ci.yml`) cover them; no wiring change.

## Risks & Mitigations

- A listing that stays incomplete (API outage, or more than 1,000 active review dispatches) keeps the checker at `retry` — ACCEPTED: that is the finding's recommendation. The §26 checker stops renewing the dead-man's switch, so the pushing session is woken within 7 days; the project checker routes to a block stage after three retries.
- More API calls on the PR-named branch (4 instead of 1 in `--hand-back` mode) — ACCEPTED: issued only after the head-branch reads found nothing, documented per §15.
- The shipped `.claude/` copy lags the twin until the supervising session syncs it — ACCEPTED: the phase PR carries a `hold` claim and the project stops at the twin-sync blocker until then (§28.C interim default).

## Rollout

Ships to consumer repos with the `.claude/` sync after `@stable`. No flag, no new env var, no state. Rollback is reverting the phase PR.

## Auto-decisions

- AD-1 [plan, 2026-09-30] Which base branch does the project build on? — Picked: A — the issue's `Integration branch:` `claude/implement-plan-issue-4701-review-dispatch-default-branch`. Alternatives: B — `main`. Why: the issue names it, and `PR_NAMED_REVIEW_RUNS_PATH` exists only on that branch. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-30] Which listing replaces the repo-wide page? — Picked: A — per wrapper and per active status (`actions/workflows/<wrapper>/runs?event=workflow_dispatch&status=<s>`), paginated to `total_count`. Alternatives: B — per wrapper within a `created` window, as the poller does since #4927; C — repo-wide `actions/runs?event=workflow_dispatch&status=<s>`. Why: only the checker's active states matter, so a status filter keeps each listing small with no time window, and only a per-wrapper listing cannot be crowded by other workflows, as the finding recommends. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What does an incomplete listing do? — Picked: A — raise `ReadError` (exit 2, `action: retry`); the sweep skips the PR. Alternatives: B — count it as an active run (`wait`). Why: both defer the hand-back, but `retry` is what every other failed read does and stops renewing the dead-man's switch, so a listing that stays incomplete is seen instead of waiting silently. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How is an HTTP 404 treated? — Picked: A — on a wrapper's first read, the wrapper is absent (complete, empty, remaining statuses skipped); on any later read, incomplete. Alternatives: B — every 404 is incomplete; C — every 404 is absent. Why: every repo lacks one of the two wrappers, so B would defer every hand-back forever; a 404 after a successful read of the same wrapper is not an absent wrapper. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Keep `PR_NAMED_REVIEW_RUNS_PATH` and `DISPATCHED_REVIEW_RUNS_PATH`? — Picked: A — keep both with their values and add the new constant beside them. Alternatives: B — replace them. Why: §6 naming immutability. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Update the "at most six further reads" wording in `CLAUDE.md` §26.C and `agents.md`? — Picked: A — yes, reword it so it no longer states a count the wrapper listings exceed. Alternatives: B — leave it. Why: §7; a stale call budget in the checker's own instructions misleads future readers. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which script do the updated tests load before the twin sync? — Picked: A — keep loading `.claude/scripts/check_in_status.py` (as #5021 did), verify the phase against the twin with a scratch copy, and let CI go green with the `[claude-twin-sync]` commit. Alternatives: B — switch both modules to load the twin. Why: §5 and the parity tests; after the sync both copies are byte-identical, so the tests exercise the shipped file. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` result: `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.

## References

- Issue #5442, security audit tracker #3576.
- Issue #4926 (PR #5021) and its plan `docs/completed/issue-4926-consumer-review-handoff-plan.md`; issue #4927 and `docs/completed/issue-4927-paginate-review-dispatch-runs-plan.md`; issue #4618 (sweep dispatch); issue #4701 (PR #4709).
