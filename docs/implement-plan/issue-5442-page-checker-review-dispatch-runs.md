# Implement-Plan Log — Page through the review wrappers' active dispatch runs in the §26 checker, and defer a hand-back when the listing is incomplete

- Plan: docs/plans/issue-5442-page-checker-review-dispatch-runs-plan.md
- Source issue: shubhodeep1/coding-workflows#5442 (https://github.com/shubhodeep1/coding-workflows/issues/5442)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
- Project branch: claude/implement-plan-issue-5442-page-checker-review-dispatch-runs   Final PR: #5453 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5471: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 PR #5471 opened twin-first (twin verified in a scratch mirror, 277 passed); hold claim posted and the twin-sync blocker is on #5442. Resume with `/reclarify` after the `[claude-twin-sync]` push; that stage arms the wait on #5471 (step 7).

## Phases
1. [ ] Phase 1 — per-wrapper, paginated, completeness-checked active dispatch count in `check_in_status.py` — protected paths: `.claude/scripts/check_in_status.py` — PR #5471 open (waiting on twin sync); review rounds: 0; interventions: 0
   - [x] `PR_NAMED_REVIEW_WORKFLOW_RUNS_PATH` (`:157`), `_pr_named_active_review_run_count` (`:326`) and `_complete_dispatch_run_listing` (`:365`) added in the twin; `_active_run_count` (`:303`) uses them; old constants kept (§6, `:136`, `:153`)
   - [x] a first-read 404 is an absent wrapper; every other incomplete listing raises `ReadError` (`test_absent_wrapper_is_read_once_and_the_other_wrapper_still_counts`, `test_incomplete_dispatch_listing_defers_the_hand_back` × 9)
   - [x] docstrings (module API budget, `_active_run_count`, both new helpers) updated
   - [x] tests: `tests/test_check_in_status.py`, `tests/test_check_in_status_hand_back.py`; `tests/test_claude_pr_sweep.py` unchanged — 277 passed against the twin in a scratch mirror
   - [x] `agents.md`, `CLAUDE.md` §26.C wording (`workflow-templates/CLAUDE.md` is a symlink to it); `changelog.d/5442-page-checker-review-dispatch-runs.md`
   - [ ] `[claude-twin-sync]` copy into `.claude/scripts/check_in_status.py` (supervising session; twin sha256 `31fee86aa4153632795c76b2b95a73d8c35cb36aa7212bfbccfeaa18fd753712`)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which base branch does the project build on? — Picked: A — the issue's `Integration branch:` `claude/implement-plan-issue-4701-review-dispatch-default-branch`. Alternatives: B — `main`. Why: the issue names it, and `PR_NAMED_REVIEW_RUNS_PATH` exists only on that branch. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-30] Which listing replaces the repo-wide page? — Picked: A — per wrapper and per active status (`actions/workflows/<wrapper>/runs?event=workflow_dispatch&status=<s>`), paginated to `total_count`. Alternatives: B — per wrapper within a `created` window, as the poller does since #4927; C — repo-wide `actions/runs?event=workflow_dispatch&status=<s>`. Why: only the checker's active states matter, so a status filter keeps each listing small with no time window, and only a per-wrapper listing cannot be crowded by other workflows, as the finding recommends. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What does an incomplete listing do? — Picked: A — raise `ReadError` (exit 2, `action: retry`); the sweep skips the PR. Alternatives: B — count it as an active run (`wait`). Why: both defer the hand-back, but `retry` is what every other failed read does and stops renewing the dead-man's switch, so a listing that stays incomplete is seen instead of waiting silently. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] How is an HTTP 404 treated? — Picked: A — on a wrapper's first read, the wrapper is absent (complete, empty, remaining statuses skipped); on any later read, incomplete. Alternatives: B — every 404 is incomplete; C — every 404 is absent. Why: every repo lacks one of the two wrappers, so B would defer every hand-back forever; a 404 after a successful read of the same wrapper is not an absent wrapper. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-30] Keep `PR_NAMED_REVIEW_RUNS_PATH` and `DISPATCHED_REVIEW_RUNS_PATH`? — Picked: A — keep both with their values and add the new constant beside them. Alternatives: B — replace them. Why: §6 naming immutability. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Update the "at most six further reads" wording in `CLAUDE.md` §26.C and `agents.md`? — Picked: A — yes, reword it so it no longer states a count the wrapper listings exceed. Alternatives: B — leave it. Why: §7; a stale call budget in the checker's own instructions misleads future readers. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Which script do the updated tests load before the twin sync? — Picked: A — keep loading `.claude/scripts/check_in_status.py` (as #5021 did), verify the phase against the twin with a scratch copy, and let CI go green with the `[claude-twin-sync]` commit. Alternatives: B — switch both modules to load the twin. Why: §5 and the parity tests; after the sync both copies are byte-identical, so the tests exercise the shipped file. Applied in: phase 1 PR. Status: pending review
- AD-8 [phase 1/1, 2026-09-30] Where does the paging of one wrapper listing live? — Picked: A — a second small helper, `_complete_dispatch_run_listing`, beside `_pr_named_active_review_run_count`. Alternatives: B — reuse `_gh_api_paginated_object` and parse the 404 page out of its error text; C — inline the page loop in the counter. Why: the 404-is-absent rule applies only to a wrapper's first page, which `_gh_api_paginated_object` cannot report, and the completeness check needs the distinct run ids; the name was checked for collisions. Applied in: PR #5471. Status: pending review

## Lessons
- [source:plan-deviation] A paged GitHub listing whose 404 means "absent" only on the first page needs its own page loop: the shared paginator's error cannot say which page failed. (files: workflow-templates/.claude/scripts/check_in_status.py)

## Notes
- Issue mode: plan written by /implement-issue-claude for #5442; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Security pass: `security_pass_skip.py` returned skip (`ai:security`: created and labelled by the issue automation).
- Base branch check (2026-09-30): the base's PR #4709 (into `main`) is open, not merged.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
