# Implement-Plan Log — Pending-checks auto-merge: defer while a newer review of the PR is active

- Plan: docs/plans/issue-5148-pending-checks-newer-review-race-plan.md
- Source issue: shubhodeep1/coding-workflows#5148
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4900-claude-fixer-pending-checks-auto-merge
- Project branch: claude/implement-plan-issue-5148-pending-checks-newer-review-race   Final PR: #5178 draft
- Status: IN_PROGRESS
- Stage: conformance 2/3
- Activation: not started
- Waiting on: conformance fix PR 2 (branch claude/implement-plan-issue-5148-pending-checks-newer-review-race-conformance-fix-2; number in the conformance 2/3 stage report)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01HRuW9tokc4gWgq1vPau85k   safety net and hand-back: ids in the conformance 2/3 stage report
- Last updated: 2026-09-29
- Last note: conformance 2/3 (session session_01NzSMeavZGfEbpnoB4y5HUu): PR #5209 merged 2026-09-29; re-audit CONFORMANT (Implemented: COMPLETE, Correctness: CONCERNS); one EVIDENCE-BASED stale-doc finding (the sweep's §15 batching contract omitted the #5148 run reads) fixed in conformance fix PR 2, docstring only.

## Phases
1. [x] Phase 1 — defer the pending-checks merge while a newer review is active or unsettled   — PR #5183 merged 2026-09-29; review rounds: 1; interventions: 0
   - `check_review_runs()` in `scripts/claude_fixer_pending_checks.py`: head-branch runs, `[pr:<N>]` internal-review dispatches, unbound `review_autofix.yml` / `ai-review.yml` dispatches
   - `evaluate()` returns `review_active` / `review_superseded` and re-reads the marker before merging
   - Tests: the audit's exploit scenario and every defer / supersede path; existing #4900 suites green
   - `README.md`, `agents.md`, `docs/INVENTORY.md`; `changelog.d/5148-pending-checks-newer-review-race.md`
   - Done: the plan's phase 1 "done" condition. Protected paths: none.

## Conformance
- Run 1 — 2026-09-29: CONFORMANT (Implemented: COMPLETE, Correctness: CONCERNS) — fix PR #5209: `review_rb_judge_dispatch.yml` added to the unbound dispatch listings (pre-security); review rounds: 3; merged 2026-09-29
- Run 2 — 2026-09-29: CONFORMANT (Implemented: COMPLETE, Correctness: CONCERNS) — conformance fix PR 2: `scripts/claude_pr_sweep.py`'s §15 batching contract now lists the pending-checks pass's head-branch runs read, 4 workflow_dispatch runs reads, comments re-read, and compare read (pre-security)

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which review runs count as a newer review of the PR? — Picked: A — every run on the PR's head branch, `internal-review.yml` dispatches bound by the `[pr:<N>]` title, and (fail closed) any active `review_autofix.yml` / `ai-review.yml` dispatch. Alternatives: B — title-bound dispatches only; C — any active workflow run in the repository. Why: covers every path the gate lets a forced review take. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What does "authorize only against the latest completed review" mean? — Picked: A — the latest completed review bound to this PR and newer than the marker's run must have concluded `success`, and the same marker must still be live on a re-read right before the merge. Alternatives: B — the marker's run must be the latest completed review; C — active runs only. Why: gate-skipped sweep dispatches conclude `success` and must not block; a failed newer review left no verdict. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which statuses count as active? — Picked: A — any status other than `completed`. Alternatives: B — `queued` / `in_progress` / `pending` only. Why: fail closed (§1). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Reuse `check_in_status._active_run_count` or read the listings directly? — Picked: A — one head-branch listing plus one dispatch listing per review workflow, reusing `check_in_status`'s constants. Alternatives: B — `_active_run_count` plus completed-run reads. Why: fewer calls (§15). Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Do the new states change the sweep's counters? — Picked: A — no; logged by the existing `pending_checks` line. Alternatives: B — count `review_active` as waiting. Why: §5. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-29] Where does the fix live? — Picked: A — in `evaluate()`. Alternatives: B — in the sweep loop. Why: the module owns the merge decision and its tests. Applied in: phase 1 PR. Status: pending review
- AD-7 [conformance 1/3, 2026-09-29] How should the pending-checks pass treat the stall poller's `force_rb_judge` runs, which come through `review_rb_judge_dispatch.yml` and are not listed? — Picked: A — add `.github/workflows/review_rb_judge_dispatch.yml` to `UNBOUND_DISPATCH_REVIEW_WORKFLOWS`: any active run defers every pending-checks merge in the repo by one tick (fail closed, one more 404-tolerant read per ready PR). Alternatives: B — bind each judge run to its PR by reading its jobs or logs (a read per run, §15); C — leave it (the run only labels a Claude-fixer PR `ai:review-blocked`). Why: the plan's AD-1 meant to cover `force_rb_judge` dispatches, and fail closed wins under §1; these dispatches are rare. Applied in: conformance fix PR. Status: pending review
- AD-8 [conformance 1/3 — review round 1, 2026-09-29] The reviewer panel (1 of 6, NIT) flagged that PR #5209 has no `docs/INVENTORY.md` hunk although the plan asks for a sentence there. How? — Picked: A — extend the existing `docs/INVENTORY.md` sentence for `scripts/claude_fixer_pending_checks.py` to name the runs it watches, including `review_rb_judge_dispatch.yml`, matching the README.md and agents.md wording this PR changed. Alternatives: B — reject it (phase 1 already added the sentence), which needs the dedicated verdict bot this web session cannot post as, so the PR would block. Why: the smallest accurate change that settles the finding without blocking the PR. Applied in: PR #5209 (review round 1 commit). Status: pending review

## Lessons
- [source:security] Before an automated job enables auto-merge from an earlier review's result, it must check for newer reviews of the same PR that are still running (including dispatches from the default branch, whose check runs never attach to the PR head) and re-read the result after that check; nothing disables auto-merge once a later review finds a problem. (files: scripts/claude_fixer_pending_checks.py)
- [source:conformance] When a guard lists workflow runs by workflow file, list the wrapper workflows too: a `workflow_dispatch` wrapper that calls a reusable workflow (such as `review_rb_judge_dispatch.yml` calling `review_autofix.yml`) records its runs under the wrapper's own path, not the reusable workflow's. (files: scripts/claude_fixer_pending_checks.py, .github/workflows/review_rb_judge_dispatch.yml)
- [source:conformance] When a module's API budget grows, update every caller that restates it too: a batching contract (CLAUDE.md §15) in the calling script's docstring goes stale silently, because no test reads it. (files: scripts/claude_pr_sweep.py, scripts/claude_fixer_pending_checks.py)

## Notes
- Conformance run 2 (2026-09-29, project head 660568d): checks run: `pytest tests/test_claude_fixer_pending_checks.py tests/test_claude_pr_sweep.py tests/test_check_in_status_hand_back.py tests/test_review_autofix_claude_fixer_mode.py tests/test_check_in_session_targeting.py` (282 passed, Python 3.11), `ruff check --select E,F --ignore E501` on the footprint (pass). Audited against `internal-review.yml`'s triggers and concurrency groups: no normal event produces a newer head-branch or dispatched review run that concludes other than `success` on an unchanged head, so `review_superseded` cannot block a clean PR in steady state.
- Conformance review round 3 (2026-09-29, head c05da49, PR #5209): the only finding (minimax, confidence 2) was valid: the `tests/test_claude_fixer_pending_checks.py` case for an unbound `review_autofix.yml` dispatch was labelled `force_rb_judge / convergence dispatch`, while `scripts/claude_fixer_pending_checks.py:120-127` now says `review_autofix.yml` covers convergence and direct dispatches and the stall poller's `force_rb_judge` path runs as `review_rb_judge_dispatch.yml` (its own case). Relabelled `convergence or direct review_autofix.yml dispatch`; no behaviour change.
- Conformance review round 2 (2026-09-29, head 08ae2d0, PR #5209): the only finding (task gap, glm, NIT) was valid: `changelog.d/5148-pending-checks-newer-review-race.md`'s reads row still said 3 `workflow_dispatch` runs listings while the module docstring says 4 after `review_rb_judge_dispatch.yml` joined them; fixed to 4. No other doc carries the count.
- Conformance review round 1 (2026-09-29, head c2ae591, PR #5209): the only finding was a task gap (no `docs/INVENTORY.md` hunk); invalid on the project (line 125 carries phase 1's sentence), settled by AD-8. The push-triggered `review-claude-branch-push / codex-agent (claude-branch-review)` check failed in its LLM summariser (`all 10 attempts failed`, run 36598991686), an infrastructure failure; the PR-event review run on the same head succeeded.
- Review round 1 (2026-09-29, head a697863): F1 (run with no `status`) fixed; the old code already failed closed but reported it as an active run. F2 (run with a non-integer `id`) fixed; the superseded check skipped such a run (fail open), while its `TypeError` claim about `marker_run_id` was wrong (always an `int`). F3 (comments re-read unvalidated) rejected: `check_in_status.gh_api_list` raises `ReadError` on a non-array page or a non-object item.
- Issue-mode project started by the Claude issue dispatcher routine (trigger trig_01REnV4f26hDKipeNXXCHKgR) in session session_01AnxtvQ843pzNkV2iTQRcJc (Auto mode). Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5148#issuecomment-5891537425
- Security pass: skip (`security_pass_skip.py`: `ai:security` created and labelled by the issue automation).
- Base branch is the #4900 project branch (final PR #4922, open at start). The final PR targets it, so the final-merge stage closes #5148 explicitly and activation is n/a.
- Stale Routine sweep (CLAUDE.md §26.G) at start: `list_triggers` was refused by the Auto-mode classifier; skipped, not blocking.
- Plan deviation (wording only): completed `internal-review.yml` dispatches are bound to the PR by their `[pr:<N>]` title alone, like `check_in_status._active_run_count`, instead of `_is_pr_dispatched_review_run`; a broader match can only block a merge. The plan text was updated in the phase 1 PR.
- Local verification ran on Python 3.11; `tests/test_workflow_retro.py` cannot import `scripts/workflow_retro.py` there (3.12 f-string syntax), unrelated to this project. CI runs 3.12.
