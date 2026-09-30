# Implement-Plan Log — Decide a stalled review's re-dispatch from a verified workflow run, not from a claim

- Plan: docs/plans/issue-5376-verify-stall-redispatch-run-plan.md
- Source issue: shubhodeep1/coding-workflows#5376 (https://github.com/shubhodeep1/coding-workflows/issues/5376)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4985-skip-marker-review-stall
- Project branch: claude/implement-plan-issue-5376-verify-stall-redispatch-run   Final PR: #5387 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: PR #5408: twin sync
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 PR #5408 opened (twin-first); hold claim posted; blocked for the `[claude-twin-sync]` copy of `check_in_status.py` and `fix-claude-pr.md` into `.claude/` (blocker comment on #5376). The stage that `/reclarify` resumes arms the wait on #5408.

## Phases
1. [ ] Phase 1 — verified-run re-dispatch rule — PR #5408 open (blocked: twin sync); review rounds: 0; interventions: 0; protected paths: `.claude/scripts/check_in_status.py`, `.claude/commands/fix-claude-pr.md` (edited through their `workflow-templates/.claude/` twins)
   - checker twin: `_dispatched_review_runs`, `_head_arrival_time`, `_verified_review_redispatch`; `stall_redispatched` from a verified run; `_active_run_count` shares the listing
   - consumer wrapper: `workflow-templates/ai-review.yml` dispatch-only `run-name`
   - fixer twin: `fix-claude-pr.md` step 3 wording
   - tests: `tests/test_check_in_status_hand_back.py` (loads the twin)
   - docs: `agents.md`, `README.md`, `changelog.d/5376-verify-stall-redispatch-run.md`
   - Done: new and changed tests pass against the twin; ruff and yamllint clean; only twin-parity tests red until `[claude-twin-sync]`

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py`: "ai:security: created and labelled by the issue automation")

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] What proves a stalled head's review was already re-dispatched? — Picked: A — a completed, not-cancelled `workflow_dispatch` review run on the default branch, titled for this PR, created at or after the head arrived. Alternatives: B — keep the claim and also require the run; C — any claim by the workflow account only. Why: the issue's recommendation; default-branch dispatched runs cannot be forged by a PR author. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] When did the head arrive? — Picked: A — the later of the committer date and the earliest check-run `started_at`. Alternatives: B — committer date only; C — earliest check-run start only. Why: backdating cannot pull an older run in. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Which run conclusions count? — Picked: A — every completed conclusion except `cancelled`. Alternatives: B — `success` only; C — all. Why: a cancelled run reviewed nothing; others would recur. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] How do consumer repos bind a dispatched run to the PR? — Picked: A — dispatch-only `run-name` `AI Review [pr:<N>]` in `workflow-templates/ai-review.yml`, with a 404 fallback to its listing. Alternatives: B — no binding, always retry; C — keep the claim rule there. Why: B loops per lease, C keeps the vulnerability. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Should the active-run count see consumer dispatched runs? — Picked: A — yes, through the same listing helper. Alternatives: B — `internal-review.yml` only. Why: a running consumer re-dispatch would otherwise be re-dispatched again. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-30] Which copy do the hand-back tests load? — Picked: A — the twin. Alternatives: B — `.claude/`, red until sync. Why: the twin-first rule. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-30] Change the `CLAUDE.md` §26.H wording? — Picked: A — no, it stays true. Alternatives: B — spell out the run rule there. Why: §5. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Issue progress comment: 5903022362.
- Invoking session: session_01GokLJ6bAqCLubeWsfg7Zch (permission mode auto).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- 2026-09-30, phase 1 (session_01GokLJ6bAqCLubeWsfg7Zch): the new hand-back tests (loading the twin) pass: 131 passed, with only the 2 twin-parity checks failing. Related suites (71 files): 2502 passed, 1 skipped. The 5 failures are 4 twin-parity checks and 1 missing `gawk`; the validation-template renderer suites need `jsonschema` and `jinja2`, which are not installed here. With the twins copied into `.claude/` (simulated sync), the 8 affected suites pass (427). ruff and yamllint are clean.
