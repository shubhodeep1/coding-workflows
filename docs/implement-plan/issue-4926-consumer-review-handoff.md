# Implement-Plan Log — Recognize consumer `ai-review.yml` PR-named dispatch runs in the §26 checker

- Plan: docs/completed/issue-4926-consumer-review-handoff-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4926 (https://github.com/shubhodeep1/coding-workflows/issues/4926)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4701-review-dispatch-default-branch
- Project branch: claude/implement-plan-issue-4926-consumer-review-handoff   Final PR: #4963 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4701-review-dispatch-default-branch)
- Waiting on: completion PR (claude/implement-plan-issue-4926-consumer-review-handoff-complete; its number is in the validation 1/3 stage report and on the #4926 progress comment)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01FBt61CKPxi32oMhWqfVKZd (reused)   safety net and hand-back in the validation 1/3 stage report
- Last updated: 2026-09-29
- Last note: validation skipped on operator answer Q1: A (covered by #4701's project validation); plan moved to docs/completed/ in the completion PR; next: final-merge 1/1.

## Phases
1. [x] Phase 1 — recognize both PR-named review wrappers in `check_in_status.py` — protected paths: `.claude/scripts/check_in_status.py` — PR #5021 merged 2026-09-29 (e316804); review rounds: 0; interventions: 0
   - [x] `PR_NAMED_REVIEW_DISPATCHES` / `PR_NAMED_REVIEW_RUNS_PATH` added; `DISPATCHED_REVIEW_*` kept (§6)
   - [x] `_is_pr_dispatched_review_run` matches (path, title) pairs for both wrappers
   - [x] `_active_run_count` reads one repo-wide `workflow_dispatch` listing and counts active runs of either pair
   - [x] `workflow-templates/.claude/scripts/check_in_status.py` identical to `.claude/scripts/check_in_status.py` — twin sync `f2a517e` (Q2: A)
   - [x] tests: `tests/test_check_in_status_hand_back.py`, `tests/test_check_in_status.py`; `tests/test_claude_pr_sweep.py` unchanged
   - [x] `changelog.d/4926-consumer-review-handoff.md`; `agents.md` verdict-helper bullet

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-validation; security skipped). Every plan criterion traced to code (`.claude/scripts/check_in_status.py:137-148`, `:294-353`), both copies identical (`cmp`), 647 tests passed across the check-in, sweep, and helper modules plus 47 changelog tests.

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation
- Skipped (covered by #4701's project validation) — operator answer Q1: A on #4926 (issuecomment-5886503812, standing decision Q17: A), 2026-09-29. `validate.yml` authorizes `target_ref` only for an open final PR into `main`, and this project's final PR #4963 targets #4701's project branch (long-term fix: #4734). #4701's chain re-runs its security audit and runtime validation on a project branch that will contain this fix before anything reaches `main`.

## Completion
- Completion PR (claude/implement-plan-issue-4926-consumer-review-handoff-complete) open 2026-09-29 — doc moved to docs/completed/issue-4926-consumer-review-handoff-plan.md
- Final PR #4963 draft (into claude/implement-plan-issue-4701-review-dispatch-default-branch)

## Activation
- n/a: the base is #4701's project branch, so this change goes live with that project (issue mode). The final-merge stage closes #4926 and labels it `ai:merged`.

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which base branch does the project build on? — Picked: A — the issue's `Integration branch:` `claude/implement-plan-issue-4701-review-dispatch-default-branch`. Alternatives: B — `main`. Why: the issue names it, and the consumer `run-name` and the #4618 checker code exist only on that branch. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-29] How does `_active_run_count` find PR-named runs of both wrappers? — Picked: A — one repo-wide `workflow_dispatch` listing matched on both (path, title) pairs. Alternatives: B — one per-wrapper listing each, with 404 counted as zero (two calls in every repo, one always a 404); C — `internal-review.yml` first, then `ai-review.yml` on 404 (two calls in every consumer repo). Why: one call in both repo kinds (§15), and the same listing and window the poller uses since #4701's AD-4. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does the repo-wide listing fail? — Picked: A — any failed read raises `ReadError` (exit 2, `action: retry`), as every other read does. Alternatives: B — fail open to zero. Why: the per-workflow 404 case no longer exists, and failing open would let a hand-off start a fixer while a review may be running. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Must the wrapper path and the title match as a pair? — Picked: A — yes, each wrapper only with its own title. Alternatives: B — accept either title from either wrapper path. Why: pairing is the tighter binding and costs nothing; each wrapper's `run-name` produces only its own title. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Keep the old `DISPATCHED_REVIEW_*` constants? — Picked: A — keep all three with their values and add the new ones beside them. Alternatives: B — replace them. Why: §6 naming immutability. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-29] How do `_is_pr_dispatched_review_run` and `_active_run_count` share the pair match? — Picked: A — one small helper, `_is_pr_named_review_pair`. Alternatives: B — duplicate the pair comparison inline in both functions. Why: one place decides what binds a run to a PR, so the two paths cannot drift; the name was checked for collisions. Applied in: PR #5021. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by /implement-issue-claude for #4926; start-up checks auto-decided (CLAUDE.md §28.A). Permission mode auto.
- Security pass: `security_pass_skip.py` returned skip (`ai:security`: created and labelled by the issue automation).
- The session that started this project had no GitHub MCP tools and no `gh` until the repo's SessionStart hook was run by hand; GitHub reads and writes went through `gh api` / `curl` via the agent proxy.
- Phase 1 blocked before it started: it must edit `.claude/scripts/check_in_status.py` (protected path) and no `Protected-path approval: phase 1` line exists. The question is on issue #4926 with `ai:claude-blocked`.
- Protected-path approval: phase 1 — D, twin-first per Q40 (2026-09-29): operator answer on #4926 (interim twin-first rule until #4785 lands). The stage edits only `workflow-templates/.claude/scripts/check_in_status.py` (and its tests), never `.claude/**`, opens the phase PR into the project branch, posts a `hold` claim, and stops BLOCKED; the supervising session copies the twin into `.claude/scripts/check_in_status.py` as `[claude-twin-sync]`, pushes (lifting the hold), and comments `/reclarify`; the chain then continues with the phase 1 wait.
- Resume session session_01Dvs4NGwErYUDtLM56onKFQ (started by the master session, operator Q14: A): merged the base `claude/implement-plan-issue-4701-review-dispatch-default-branch` into the project branch (clean, 478b115); the base's PR #4709 is still open.
- Conformance 1/3 stage session session_01VftDFUVcmqcuzJE1Pgdhet: merged the base into the project branch again (clean, 539e9a6); conformance CONFORMANT; validation blocked before dispatch (`validate.yml` target authorization needs a final PR into `main`), asked as Q1 on #4926 with `ai:claude-blocked`; answered Q1: A by the operator (Q17: A); label removed; completion PR opened.
