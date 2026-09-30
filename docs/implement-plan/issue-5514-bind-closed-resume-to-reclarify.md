# Implement-Plan Log — Bind a closed-issue final-merge resume to its triggering /reclarify

- Plan: docs/completed/issue-5514-bind-closed-resume-to-reclarify-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5514 (https://github.com/shubhodeep1/coding-workflows/issues/5514)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5222-final-merge-resume-closed-issue
- Project branch: claude/implement-plan-issue-5514-bind-closed-resume-to-reclarify   Final PR: #5544 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation (issue base is not the default branch: set to n/a (base claude/implement-plan-issue-5222-final-merge-resume-closed-issue) once the final PR merges)
- Waiting on: completion PR
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01LQLfXaYQEafXgrfxYiScwa   safety net and hand-back: in the stage report
- Last updated: 2026-09-30
- Last note: validation cycle 1 (run 36699022910) passed 10/10 on the project branch; completion PR moves the plan to docs/completed/, next stage final-merge

## Phases
1. [x] Phase 1 — bind the closed-issue resume to its /reclarify comment   — PR #5557 merged 2026-09-30; review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT — no fixes (pre-security; security skipped)

## Security pass
- Skipped (ai:security: automation-produced issue; security_pass_skip.py verified)

## Validation
- Cycle 1 — run 36699022910 2026-09-30 (target_ref: claude/implement-plan-issue-5514-bind-closed-resume-to-reclarify): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 295s)

## Completion
- Completion PR (this PR) — doc moved to docs/completed/issue-5514-bind-closed-resume-to-reclarify-plan.md
- Final PR #5544 draft (into claude/implement-plan-issue-5222-final-merge-resume-closed-issue)

## Activation
- n/a: the issue base is not the default branch; the change goes live with PR #5225's lifecycle

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should the closed-issue resume be bound? — Picked: A — bind it to the triggering `/reclarify` comment ID (payload `reclarify_comment_id` / clarify's event comment), which must be a trusted `/reclarify` after the latest blocked comment and created strictly after `closed_at`; refuse payloads without it. Alternatives: B — require any trusted `/reclarify` after `closed_at`, with no ID; C — remove the intake's closed-issue path. Why: A is the finding's recommendation and closes both replay paths; B leaves `opened`/`manual` replays that ride a real post-closure comment; C breaks #5222. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What should the new payload key be called? — Picked: A — `reclarify_comment_id`, optional, added only when set. Alternatives: B — `comment_id`; C — `trigger_comment_id`. Why: §6 uniqueness; `comment_id` is already a nested parameter in `final_merge_resume`, and `TRIGGER_COMMENT_ID` is an existing env name in `plan.yml`. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Is a `/reclarify` in the same second as `closed_at` after the closure? — Picked: A — no, require `created_at` strictly after `closed_at`. Alternatives: B — accept equal timestamps. Why: §1 fail-closed; timestamps have one-second resolution, so an equal one may predate the close. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] May the intake's `workflow_dispatch` resume a closed issue? — Picked: A — no, a `manual` payload for a closed issue is refused `issue_closed`. Alternatives: B — add a comment-ID input to `workflow_dispatch`. Why: the finding asks to reject payloads without provenance; `/reclarify` is the resume path; §5. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Should the session-side check in `/implement-issue-claude` step 2 also require a post-closure `/reclarify`? — Picked: A — no, leave the protected command unchanged. Alternatives: B — edit the `workflow-templates/.claude/` twin and stop for the twin sync. Why: a session for a closed issue starts only from a queue item the intake authorized, so the intake is the boundary the finding names; §5, and no protected-path stop. Applied in: no code change. Status: pending review
- AD-6 [plan, 2026-09-30] Which reason does the intake report for the new refusals? — Picked: A — keep `issue_closed` for every closed-issue refusal; the detailed reason shows in clarify's `final_merge_resume … reason=` notice. Alternatives: B — new intake refusal reasons. Why: §5; README, agents.md, and tests document `issue_closed` as the closed-issue refusal. Applied in: phase 1. Status: pending review

## Lessons
- [source:plan-deviation] A cloud session runner may lack `gawk`, which `scripts/review_issue_ledger.sh` needs; install it (`apt-get install -y gawk`) before judging `tests/test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean` red. (files: scripts/review_issue_ledger.sh)

## Notes
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5514#issuecomment-5906441078
- Invoking session: session_01RPPtrKP5x76mZs3mNmC2u5 (started by the Claude issue pickup, routine "implement-issue #5514").
- Base check (issue mode): the issue base's own final PR #5225 is open (draft), so the base has not moved.
- Reproduced the finding before the fix: on the base code `authorize_target` authorized `opened`, `manual`, and a replayed pre-closure bound `/reclarify` for a closed final-merge issue; after the fix all three are refused `issue_closed`.
