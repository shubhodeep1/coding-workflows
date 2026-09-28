# Implement-Plan Log — Dispatch the review sweep from the default branch only

- Plan: docs/plans/issue-4618-sweep-dispatch-default-branch-plan.md
- Source issue: shubhodeep1/coding-workflows#4618 (https://github.com/shubhodeep1/coding-workflows/issues/4618)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4618-sweep-dispatch-default-branch   Final PR: #4634 draft
- Status: BLOCKED
- Stage: conformance 2/3 — fix PR
- Activation: not started
- Waiting on: twin sync on conformance fix PR 2 (claude/implement-plan-issue-4618-sweep-dispatch-default-branch-conformance-fix-2): the supervising session copies `workflow-templates/.claude/scripts/check_in_status.py` into `.claude/scripts/`, pushes, and comments `/reclarify`
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (project checker session_0127Nu8u91wXmEAYabPuSQNQ idle, kept for reuse)
- Last updated: 2026-09-28
- Last note: conformance run 2 CONFORMANT (Correctness: CONCERNS): check_pr's stuck path did not count sweep-dispatched runs; fix written twin-first (operator Q1: A, 2026-09-28) in conformance fix PR 2, held for the supervising session's `[claude-twin-sync]` of `.claude/scripts/check_in_status.py`

## Phases
1. [x] Phase 1 — default-branch sweep dispatch with PR-keyed dedupe   — PR #4638 merged 2026-09-28 (by hand after Q1: A); review rounds: 1; interventions: 0
   - [x] `internal-review.yml` names `workflow_dispatch` runs `Internal: AI Review & Autofix [pr:<N>]`
   - [x] `review_autofix_sweep.yml` dispatches without `--ref`, validates `pr_number`, keys dispatch runs by `pr:<N>`
   - [x] `_has_active_autofix_run` sees active PR-named dispatch runs
   - [x] tests: `test_conflict_dispatch_active_run_visibility.py`, `test_review_autofix_sweep_stale_queued.py`
   - [x] `agents.md` and `changelog.d/4618-sweep-dispatch-default-branch.md`

## Conformance
- Run 1 — 2026-09-28: INCOMPLETE — fix PR #4703 (pre-security): `.claude/scripts/check_in_status.py:381` rejected every hand-off whose review run the sweep dispatched from the default branch (head_branch `main`), and `_active_run_count` did not see those runs. #4703 review rounds: 1 (2 findings rejected, 2026-09-28); merged 2026-09-28 by hand (operator decision, 465ac16)
- Run 2 — 2026-09-28: CONFORMANT (Correctness: CONCERNS) — fix PR pending (pre-security): `check_pr`'s stuck path (`check_in_status.py:276`) called `_active_run_count` without `pr_number`, so it did not count sweep-dispatched runs

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which dispatch sites does the fix cover? — Picked: A — the sweep dispatch the finding names, plus PR-named run detection in the poller's `_has_active_autofix_run` so its dedupe keeps seeing sweep runs. Alternatives: B — also move `_dispatch_review_for_conflicts`, `review_merge_train.sh`, and the forward-merge fallback to the default branch; C — the sweep only, leaving the poller blind to sweep runs. Why: A closes the reported path without reintroducing the PR #3895 duplicate loop; B would also need the PR-named run in the consumer `ai-review.yml` template and a sync before its dedupe works. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] How are active runs deduplicated by PR? — Picked: A — a `workflow_dispatch` `run-name` of `Internal: AI Review & Autofix [pr:<N>]`, matched exactly together with `event == workflow_dispatch`. Alternatives: B — read each active run's jobs or inputs (one API call per run, §15); C — drop the active-run guard and rely on the concurrency groups (brings back the PR #3895 pending-run churn). Why: no new API calls in the sweep, and the repo already identifies release runs this way. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Pass the head SHA as data as well as the PR number? — Picked: A — pass only the validated PR number; `review_autofix.yml` already resolves and pins the live PR head in its gate and checkout. Alternatives: B — add a `head_sha` input to `internal-review.yml` and thread it through `review_autofix.yml`. Why: the security fix is the ref the workflow file is loaded from; B changes the reusable workflow's interface (§5, §6) without closing more of the finding. Applied in: phase 1 PR. Status: pending review
- AD-4 [conformance 1/3, 2026-09-28] A sweep-dispatched review of a same-repo PR now builds its Semble index and README static context from the default-branch tree. `review_autofix.yml` runs "Build semble index" before "Checkout PR head branch", and `scripts/build_semble_wrapper.sh:60` indexes `GITHUB_WORKSPACE`. Should `review_autofix.yml` change? — Picked: A — leave `review_autofix.yml` unchanged, and correct the changelog fragment's "reviews behave as before" to say so. Alternatives: B — rebuild the index after the PR-head checkout (changes the reusable workflow every consumer runs); C — leave the changelog as it is. Why: the plan's non-goal and §5. The index is optional and fail-soft, fork PRs have always been reviewed this way, and reviewers still read the PR head. Applied in: conformance fix PR 1. Status: pending review

## Lessons
- [source:conformance] a change to how a workflow is dispatched must be checked against every reader that identifies its runs by head_branch/head_sha (files: .claude/scripts/check_in_status.py, .github/workflows/review_autofix_sweep.yml)
- [source:conformance] when a fix adds an argument to a shared helper, update every caller of that helper, not only the one the finding names (files: .claude/scripts/check_in_status.py)

## Notes
- Protected-path approval: conformance 1/3 fix — operator Q1: A (2026-09-28): the fix to `.claude/scripts/check_in_status.py` (+ its `workflow-templates/` twin) is written in the operator's watched supervising session (session_01VwSvLnEGmUoaQD42DKapiU), which opens the fix PR and comments `/reclarify`; the chain continues at conformance 2/3.
- Noted, not fixed (outside AD-1's scope): the poller's stall-judge run list (`scripts/orchestrate_poll_process.sh:14256`) and its retrigger lookup (`:13724`) are keyed by head branch, so they don't see sweep-dispatched runs.
- Issue mode: plan written by /implement-issue-claude for #4618; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Second all-rejected Claude-fixer round blocked on the missing verdict bot (#4703, after #4638); the operator merged #4703 by hand (465ac16).
- main synced into the project branch as 83c0dde (tests/test_check_in_status_hand_back.py conflict: both appended test blocks kept) and as cfd474f (clean) on 2026-09-28.
- Protected-path approval: conformance 2/3 fix — operator Q1: A (2026-09-28), interim twin-first rule (Q40: A) until #4785 lands: the stage edits only `workflow-templates/.claude/scripts/check_in_status.py`, never `.claude/**`, opens the fix PR into the project branch, posts a `hold` claim, and stops BLOCKED; the supervising session copies the twin into `.claude/scripts/check_in_status.py` as `[claude-twin-sync]`, pushes (lifting the hold), and comments `/reclarify`; the chain then continues at conformance 3/3.
