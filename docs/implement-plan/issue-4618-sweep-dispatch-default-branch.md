# Implement-Plan Log — Dispatch the review sweep from the default branch only

- Plan: docs/completed/issue-4618-sweep-dispatch-default-branch-plan.md (moved from docs/plans/ by the completion PR)
- Source issue: shubhodeep1/coding-workflows#4618 (https://github.com/shubhodeep1/coding-workflows/issues/4618)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4618-sweep-dispatch-default-branch   Final PR: #4634 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR (claude/implement-plan-issue-4618-sweep-dispatch-default-branch-complete) into the project branch; then final PR #4634 into main
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_0127Nu8u91wXmEAYabPuSQNQ   safety net and hand-back: armed by the completion stage (ids in its report)
- Last updated: 2026-09-29
- Last note: validation cycle 1 passed (run 36424035746, 10/10 tests, target_ref: project branch); main synced into the project branch (739c773); the completion PR moves the plan to docs/completed/

## Phases
1. [x] Phase 1 — default-branch sweep dispatch with PR-keyed dedupe   — PR #4638 merged 2026-09-28 (by hand after Q1: A); review rounds: 1; interventions: 0
   - [x] `internal-review.yml` names `workflow_dispatch` runs `Internal: AI Review & Autofix [pr:<N>]`
   - [x] `review_autofix_sweep.yml` dispatches without `--ref`, validates `pr_number`, keys dispatch runs by `pr:<N>`
   - [x] `_has_active_autofix_run` sees active PR-named dispatch runs
   - [x] tests: `test_conflict_dispatch_active_run_visibility.py`, `test_review_autofix_sweep_stale_queued.py`
   - [x] `agents.md` and `changelog.d/4618-sweep-dispatch-default-branch.md`

## Conformance
- Run 1 — 2026-09-28: INCOMPLETE — fix PR #4703 (pre-security): `.claude/scripts/check_in_status.py:381` rejected every hand-off whose review run the sweep dispatched from the default branch (head_branch `main`), and `_active_run_count` did not see those runs. #4703 review rounds: 1 (2 findings rejected, 2026-09-28); merged 2026-09-28 by hand (operator decision, 465ac16)
- Run 2 — 2026-09-28: CONFORMANT (Correctness: CONCERNS) — fix PR #4811 (pre-security): `check_pr`'s stuck path (`check_in_status.py:276`) called `_active_run_count` without `pr_number`, so it did not count sweep-dispatched runs. #4811 review rounds: 1 (4 findings on 988e2be claimed the root `.claude/scripts/check_in_status.py` lacked the fix; stale, the merged root and twin are byte-identical with `pr_number=number` at `:276`); merged 2026-09-28 by hand (operator)
- Run 3 — 2026-09-28: CONFORMANT (Correctness: CONCERNS) — no fix PR (pre-security): head_branch-keyed run readers outside `check_in_status.py` (`scripts/review_merge_train.sh:407`, `scripts/gh_helpers.sh:1219`, the poller lookups noted at run 1) miss sweep runs; bounded to one redundant queued review, AD-5 keeps them unchanged

## Security pass
- Skipped (plan header: ai:security: automation-produced issue)

## Validation
- Cycle 1 — run 36424035746 2026-09-28 (target_ref: claude/implement-plan-issue-4618-sweep-dispatch-default-branch): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 277s); completed 2026-09-29

## Completion
- Completion PR (claude/implement-plan-issue-4618-sweep-dispatch-default-branch-complete) opened 2026-09-29 — doc moved to docs/completed/issue-4618-sweep-dispatch-default-branch-plan.md
- Final PR #4634 draft — marked ready at final-merge

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] Which dispatch sites does the fix cover? — Picked: A — the sweep dispatch the finding names, plus PR-named run detection in the poller's `_has_active_autofix_run` so its dedupe keeps seeing sweep runs. Alternatives: B — also move `_dispatch_review_for_conflicts`, `review_merge_train.sh`, and the forward-merge fallback to the default branch; C — the sweep only, leaving the poller blind to sweep runs. Why: A closes the reported path without reintroducing the PR #3895 duplicate loop; B would also need the PR-named run in the consumer `ai-review.yml` template and a sync before its dedupe works. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] How are active runs deduplicated by PR? — Picked: A — a `workflow_dispatch` `run-name` of `Internal: AI Review & Autofix [pr:<N>]`, matched exactly together with `event == workflow_dispatch`. Alternatives: B — read each active run's jobs or inputs (one API call per run, §15); C — drop the active-run guard and rely on the concurrency groups (brings back the PR #3895 pending-run churn). Why: no new API calls in the sweep, and the repo already identifies release runs this way. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] Pass the head SHA as data as well as the PR number? — Picked: A — pass only the validated PR number; `review_autofix.yml` already resolves and pins the live PR head in its gate and checkout. Alternatives: B — add a `head_sha` input to `internal-review.yml` and thread it through `review_autofix.yml`. Why: the security fix is the ref the workflow file is loaded from; B changes the reusable workflow's interface (§5, §6) without closing more of the finding. Applied in: phase 1 PR. Status: pending review
- AD-4 [conformance 1/3, 2026-09-28] A sweep-dispatched review of a same-repo PR now builds its Semble index and README static context from the default-branch tree. `review_autofix.yml` runs "Build semble index" before "Checkout PR head branch", and `scripts/build_semble_wrapper.sh:60` indexes `GITHUB_WORKSPACE`. Should `review_autofix.yml` change? — Picked: A — leave `review_autofix.yml` unchanged, and correct the changelog fragment's "reviews behave as before" to say so. Alternatives: B — rebuild the index after the PR-head checkout (changes the reusable workflow every consumer runs); C — leave the changelog as it is. Why: the plan's non-goal and §5. The index is optional and fail-soft, fork PRs have always been reviewed this way, and reviewers still read the PR head. Applied in: conformance fix PR 1. Status: pending review
- AD-5 [conformance 3/3, 2026-09-28] Three more readers still identify review runs by `head_branch`, so they miss sweep runs dispatched from `main`: the merge-train release guard (`scripts/review_merge_train.sh:407`), the review workflow's retrigger peer check (`scripts/gh_helpers.sh:1219`, called at `review_autofix.yml:6317` / `:6524`), and the poller lookups noted at conformance 1. Extend them in this project? — Picked: A — leave them unchanged and list them as a known limitation in the final PR #4634's body. Alternatives: B — extend all of them to PR-named dispatch runs in a conformance fix PR (changes the consumer-synced `gh_helpers.sh` and adds an API read to its peer check); C — extend only the merge-train guard (no new API call). Why: each miss costs at most one redundant review that queues behind the running sweep run (`internal-review-dispatch-pr-<N>` / `pr-autofix-<N>`, `cancel-in-progress: false`), the cost the plan's Risks section already accepts; none leaves a PR stuck, unlike the `check_in_status.py` readers conformance runs 1–2 fixed; AD-1 scoped the fix to the sweep and the poller guard. Applied in: no code change. Status: pending review

## Lessons
- [source:conformance] a change to how a workflow is dispatched must be checked against every reader that identifies its runs by head_branch/head_sha (files: .claude/scripts/check_in_status.py, .github/workflows/review_autofix_sweep.yml)
- [source:conformance] when a fix adds an argument to a shared helper, update every caller of that helper, not only the one the finding names (files: .claude/scripts/check_in_status.py)

## Notes
- Known limitation for final PR #4634's body (AD-5): `scripts/review_merge_train.sh:407`, `scripts/gh_helpers.sh:1219`, and the poller lookups (`scripts/orchestrate_poll_process.sh:14256`, `:13724`) still key review runs by head branch and miss sweep runs dispatched from `main`; each miss costs at most one redundant queued review.
- Protected-path approval: conformance 1/3 fix — operator Q1: A (2026-09-28): the fix to `.claude/scripts/check_in_status.py` (+ its `workflow-templates/` twin) is written in the operator's watched supervising session (session_01VwSvLnEGmUoaQD42DKapiU), which opens the fix PR and comments `/reclarify`; the chain continues at conformance 2/3.
- Noted, not fixed (outside AD-1's scope): the poller's stall-judge run list (`scripts/orchestrate_poll_process.sh:14256`) and its retrigger lookup (`:13724`) are keyed by head branch, so they don't see sweep-dispatched runs.
- Issue mode: plan written by /implement-issue-claude for #4618; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Second all-rejected Claude-fixer round blocked on the missing verdict bot (#4703, after #4638); the operator merged #4703 by hand (465ac16).
- main synced into the project branch as 83c0dde (tests/test_check_in_status_hand_back.py conflict: both appended test blocks kept) and as cfd474f (clean) on 2026-09-28.
- Protected-path approval: conformance 2/3 fix — operator Q1: A (2026-09-28), interim twin-first rule (Q40: A) until #4785 lands: the stage edits only `workflow-templates/.claude/scripts/check_in_status.py`, never `.claude/**`, opens the fix PR into the project branch, posts a `hold` claim, and stops BLOCKED; the supervising session copies the twin into `.claude/scripts/check_in_status.py` as `[claude-twin-sync]`, pushes (lifting the hold), and comments `/reclarify`; the chain then continues at conformance 3/3.
