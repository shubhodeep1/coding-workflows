# Pending-checks auto-merge: any failed newer review blocks the merge

Source issue: shubhodeep1/coding-workflows#5904 (https://github.com/shubhodeep1/coding-workflows/issues/5904)
Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
Security pass: skip (ai:security: automation-produced issue)

## Summary

The `claude-pr-catch-all` sweep's pending-checks pass (`scripts/claude_fixer_pending_checks.py`, issue #4900) only checks the **latest** completed review run of the PR that is newer than the marker's run (`check_review_runs`, line 503). A forced re-review that fails without posting a hand-off is masked by any later routine dispatch that the review gate skips, because a gate-skipped run concludes `success`. The older clean marker then authorizes auto-merge despite the failed re-review. This plan makes **every** newer bound review run that did not conclude `success` block the merge, so only a fresh marker from a newer successful full review can authorize it.

## Context

- Issue #5904 (security audit finding `failed-review-masked-by-skipped-run`, A01:2021 Broken Access Control, high, confidence 9/10), tracker #3576.
- `scripts/claude_fixer_pending_checks.py` on the base branch, `check_review_runs()` lines 499–508:
  ```python
  bound_reviews = [run for run in branch_runs if _run_path(run) in check_in_status.FIXER_WORKFLOW_PATHS] + bound_dispatches
  newer = [run for run in bound_reviews if run["id"] > marker_run_id]
  if newer:
  	latest = max(newer, key=lambda run: run["id"])
  	if latest.get("conclusion") != "success":
  		return {"state": "review_superseded", ...}
  ```
  `tests/test_claude_fixer_pending_checks.py` pins the masking as intended behaviour: `test_settled_reviews_let_the_merge_through` case `"a failed newer review then a successful one"` expects `merge_enabled`.
- Issue #5148 (project merged into this base branch as PR #5178) added `check_review_runs`. Its AD-2 ("the latest completed review bound to this PR that is newer than the marker's run must have concluded `success`") is the rule this plan tightens. Its rejected alternative B (the marker's run must be the latest completed review) would block every PR forever behind the gate-skipped 30-minute sweep dispatches; this plan does not do that, since a newer `success` run neither blocks nor unblocks.
- Why the tightened rule loses no legitimate merge: a newer review that actually ran the panel and succeeded always leaves its own result. Findings or a conflict post a hand-off (`find_pending_marker` then returns no marker for the head). A clean review with checks still running posts a new pending-checks marker whose run id is that newer run, so the earlier failed run is older than the new marker and no longer counted. A clean review with checks ready enables auto-merge itself (the evaluator then reports `not_eligible`).
- The review gate (`.github/workflows/review_autofix.yml`, `gate_claude_pending_checks_on_head`) skips routine `workflow_dispatch` runs on a pending-checks head bound to the current base; `force_rb_judge` dispatches and the `force-review` label or `[force-review]` title bypass it.
- CLAUDE.md §1 (security first), §5 (minimal change), §6 (no renames; the `review_superseded` state and log keys stay), §12 is not in play, §15 (no new API calls), §20 (changelog fragment), §28 (issue mode; auto-decisions below).

## Goals

- G1. `check_review_runs()` returns `review_superseded` when **any** completed review run bound to the PR (a head-branch run of `check_in_status.FIXER_WORKFLOW_PATHS`, or an `internal-review.yml` dispatch titled `[pr:<N>]`) with an id above the marker's run id concluded anything but `success`, whatever newer runs followed it. The reason names the failed run(s).
- G2. The exploit sequence from the issue (a newer failed review, then a newer gate-skipped `success` dispatch, then green checks) returns `review_superseded` and makes no merge call.
- G3. A failed review followed by a newer marker whose run is newer than the failed run (a successful full review) still merges.
- G4. No new GitHub API calls (§15): the decision reuses the listings `check_review_runs` already reads.
- G5. Docs that state the old "latest newer review" rule (module and function docstrings, `README.md`, `agents.md`, the test module docstring) state the new one, and a `changelog.d/` fragment records the security fix.

## Non-goals

- No change to the review gate in `review_autofix.yml` (AD-2): a pending-checks head with a failed newer review stays skipped by routine dispatches; recovery is a push, a base change, or a forced review.
- No change to how completed **unbound** dispatches (`review_autofix.yml`, `ai-review.yml`, `review_rb_judge_dispatch.yml` `workflow_dispatch` runs) are treated (AD-3): they carry no PR binding, and active ones already block.
- No detection of gate-skipped runs through job or step reads (AD-1, alternative B).
- No change to `find_pending_marker`, the base binding (#5147), the check snapshot, `verify_review_run`, or the sweep.

## Constraints

- §1: fail closed; when in doubt the PR does not merge.
- §5: change only the superseded decision in `check_review_runs()` plus the docs and tests that describe it.
- §6: `review_superseded`, the `pending_checks` log line, and every function name stay as they are.
- §15: zero added reads; the per-PR budget in the module docstring is unchanged.
- §20: one fragment, `changelog.d/5904-failed-review-masked-by-skipped-run.md`, section `security`.
- §9: tabs in Python, as the file already uses.

## Approach

In `check_review_runs()`, replace "the newest newer bound review must be `success`" with "no newer bound review may be anything but `success`":

```python
newer = [run for run in bound_reviews if run["id"] > marker_run_id]
unsuccessful = sorted((run for run in newer if run.get("conclusion") != "success"), key=lambda run: run["id"])
if unsuccessful:
	return {"state": "review_superseded", "reason": ...}  # names each failed run id, path, and conclusion
```

The id filter stays: the marker's own run is the review it records and older runs came before it. A newer gate-skipped `success` run is never a review result: it can neither clear an earlier failure nor block on its own. Update the comment above the filter, the function docstring's `review_superseded` paragraph, the module docstring's fail-closed list, `README.md`, `agents.md`, and the test module docstring.

Alternatives are recorded under [Auto-decisions](#auto-decisions).

## Phases & Merge Strategy

Issue mode (CLAUDE.md §28.A) authorises a single-phase plan.

1. **Phase 1 — any unsuccessful newer bound review blocks the pending-checks merge.** Files: see [Files & Modules](#files--modules). Done when `check_review_runs()` returns `review_superseded` for every failing case in [Tests](#tests) with no merge call, the merging cases still merge, the existing #4900 / #5147 / #5148 suites pass, the docs and the changelog fragment are updated, and the repo's CI passes. Rollback: revert the PR; the pass goes back to checking only the latest newer review (the masking returns, nothing else changes).

## Implementation Steps

Phase 1:
1. `scripts/claude_fixer_pending_checks.py` `check_review_runs()` (lines ~499–508): block on every newer bound review run whose conclusion is not `success`; the reason lists each such run (`<id> (<path>) concluded <conclusion>`), oldest first, and says that only a newer marker from a successful full review clears it. Update the comment above the id filter and the docstring's `review_superseded` paragraph (lines ~468–476).
2. Same file, module docstring (lines ~56–57): "no completed review run bound to this PR that is newer than the marker's run concluded anything but `success` (issue #5904: a later gate-skipped `success` does not clear an earlier failure)".
3. `tests/test_claude_fixer_pending_checks.py`: move `"a failed newer review then a successful one"` from the merging cases to the superseded cases; add the cases in [Tests](#tests); update the module docstring's #5148 sentence.
4. `README.md` (~line 1487) and `agents.md` (~lines 1076–1078): the superseded rule now reads "any newer completed review of the PR that did not succeed", plus one sentence on recovery (a push, a base change, or a `force-review` label sends the head through a new review).
5. `changelog.d/5904-failed-review-masked-by-skipped-run.md` [new], `<!-- changelog: security -->`, per §20.D.

## Files & Modules

- `scripts/claude_fixer_pending_checks.py`
- `tests/test_claude_fixer_pending_checks.py`
- `README.md`, `agents.md`
- `changelog.d/5904-failed-review-masked-by-skipped-run.md` [new]

## Tests

Unit tests in `tests/test_claude_fixer_pending_checks.py` (stub `gh`, existing helpers):
- Exploit: an `internal-review.yml` dispatch titled `[pr:42]` newer than the marker's run concluded `failure`, then a newer one concluded `success` (gate-skipped) → `review_superseded`, the reason names the failed run, no merge call.
- Same with the failure on the head branch (`_run(RUN_ID + 1, conclusion="failure")`) and the `success` a later dispatch → `review_superseded`.
- A `cancelled` newer review followed by a newer `success` → `review_superseded`.
- Several failures among newer runs → `review_superseded` naming each.
- A failed newer review, then a newer pending-checks marker whose run (`review_run` with a higher id, linked from the new comment) is newer than the failed run → `merge_enabled`.
- Unchanged and still passing: an older failed review merges; a newer non-review run failure on the head branch merges; a failed dispatch for another PR merges; finished unbound dispatches merge; a newer gate-skipped `success` alone merges; every `review_active` case.
- Suites run: `tests/test_claude_fixer_pending_checks.py`, `tests/test_claude_pr_sweep.py`, `tests/test_check_in_status_hand_back.py`, `tests/test_review_autofix_claude_fixer_mode.py`, plus the repo's standard checks.

## Risks & Mitigations

- A newer review that failed for an infrastructure reason (gate step crash, runner loss) now blocks until someone acts, while routine sweep dispatches stay gate-skipped on that head. ACCEPTED (AD-2) — fail closed per the issue's recommendation; the sweep logs `pending_checks … state=review_superseded` with the failed run every hour, and a push, a base change, or the `force-review` label sends the head through a new full review. The previous code reached the same stuck state whenever the failed run was the latest one; it was only cleared by the masking this plan removes.
- A queued run that GitHub's concurrency cancels in favour of a newer one now blocks. ACCEPTED — PR-backed review runs use `cancel-in-progress: false`, so this needs two pending runs in one group; recovery as above.
- A failed completed unbound dispatch (no PR binding) never blocks. ACCEPTED (AD-3) — unchanged behaviour; blocking on it would stall every pending merge in the repository; `review_rb_judge_dispatch.yml` labels the PR `ai:review-blocked` when it acts on a Claude-fixer head, which makes it `not_eligible`.

## Rollout

Ships when the #4900 project's final PR (#4922) merges into `main` (the sweep runs from `main`); nothing to configure. Kill switches unchanged: `CLAUDE_FIXER_ENABLED=false` and `ENABLE_AUTO_MERGE=false`. Rollback: revert.

## Auto-decisions

- AD-1 [plan, 2026-10-01] How should a later `success` run relate to an earlier failed newer review? — Picked: A — every completed bound review run newer than the marker's run must have concluded `success`; one that did not blocks until a newer marker (from a successful full review) moves the marker's run id past it. Alternatives: B — keep the latest-run rule but detect and ignore gate-skipped runs by reading each run's jobs (more API calls per PR, and it depends on job names staying stable); C — require the marker's run to be the latest completed review (blocks every PR forever behind the gate-skipped sweep dispatches; rejected as #5148 AD-2 B). Why: it is the issue's recommendation, needs no new reads (§15), and loses no legitimate merge because every successful full review leaves its own marker, hand-off, or auto-merge. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-10-01] Should the review gate also stop skipping routine dispatches on a pending-checks head with a failed newer review, so the PR recovers on its own? — Picked: A — no; change only the evaluator and document the recovery (a push, a base change, or the `force-review` label). Alternatives: B — teach `gate_claude_pending_checks_on_head` in `review_autofix.yml` to list the PR's review runs and not skip after a failed newer one (a workflow change with new reads in every gated dispatch). Why: §5 and the issue's scope; the stuck state already existed when the failed run was the latest. Applied in: phase 1 PR (docs only). Status: pending review
- AD-3 [plan, 2026-10-01] Should a completed unbound review dispatch that failed (`review_autofix.yml`, `ai-review.yml`, `review_rb_judge_dispatch.yml`) also block? — Picked: A — no, unchanged. Alternatives: B — block on any newer failed unbound dispatch in the repository. Why: those runs carry no PR binding, so B would stall every pending merge in the repo on any unrelated failure; active ones already block (#5148 AD-1). Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-01] Which changelog section? — Picked: A — `security`. Alternatives: B — `fixed`. Why: it closes an access-control finding from the security audit. Applied in: phase 1 PR. Status: pending review

## Notes

- `security_pass_skip.py` reported `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- The issue body names the base branch in its `Integration branch:` line; the project's final PR targets that branch (open draft PR #4922 into `main`).

## References

- Issue #5904; security tracker #3576; issue #5148 and its plan `docs/completed/issue-5148-pending-checks-newer-review-race-plan.md`; issue #5147; issue #4900 and its plan `docs/plans/issue-4900-claude-fixer-pending-checks-auto-merge-plan.md`.
