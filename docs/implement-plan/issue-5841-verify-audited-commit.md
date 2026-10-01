# Implement-Plan Log — Read-result stages accept only a run that covered the project's current code

- Plan: docs/plans/issue-5841-verify-audited-commit-plan.md
- Source issue: shubhodeep1/coding-workflows#5841
- Repo: shubhodeep1/coding-workflows   Default branch: main   Issue base: claude/implement-plan-issue-5016-dispatch-exact-run-id
- Project branch: claude/implement-plan-issue-5841-verify-audited-commit   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: project branch opened from the issue base; phase 1/1 starting twin-first.

## Phases
1. [ ] Phase 1 — same-ref fallback candidates; ref-and-commit check before any verdict in steps 9–10
   - protected paths: `.claude/scripts/dispatch_workflow.py`, `.claude/commands/implement-plan-claude.md` (twins: `workflow-templates/.claude/scripts/dispatch_workflow.py`, `workflow-templates/.claude/commands/implement-plan-claude.md`)
   - [ ] helper twin: `new_runs(..., ref=None)` keeps only runs whose `head_branch` is the dispatched ref; `dispatch` passes it
   - [ ] `tests/test_dispatch_workflow.py`: same-ref filter tests against the twin
   - [ ] command twin: step 2 pre-sync head; steps 9–10 ref + audited-commit rule, all candidates, one re-dispatch; Dispatch helper paragraph
   - [ ] `tests/test_implement_plan_claude_command.py`: twin-reading tests for steps 2, 9, 10
   - [ ] `agents.md`, `changelog.d/5841-verify-audited-commit.md`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] How should fallback candidates be correlated with this dispatch? — Picked: A — the helper counts only new runs whose `head_branch` is the ref it dispatched on; the read-result stage then requires the target ref and the audited commit. Alternatives: B — a correlation input echoed into each workflow's `run-name` (a workflow and consumer-wrapper change that #5016 AD-1/AD-6 rejected); C — keep the ref-only check. Why: uses data the helper already reads (§15) and blocks a run started from another ref's workflow file, with no workflow change (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] Which project head must the audited commit match? — Picked: A — the head the read-result stage finds on origin before its step 2 sync merge; a sync that needed a conflict resolution counts as a mismatch. Alternatives: B — the head after the sync; C — a head the dispatching stage records at dispatch. Why: a clean sync adds only default-branch commits, which a branch audit's merge-base range excludes, while B would re-audit nearly every time the default branch moved and C would accept code merged into the project branch after the dispatch. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] Does the commit check apply to a run GitHub named (`matched_by: dispatch_response`)? — Picked: A — yes, to every recorded run. Alternatives: B — only to unverified matches. Why: an exactly matched run still covers whatever head it resolved when it started, so one rule for every match keeps the gate simple and strict (§1). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-10-01] What happens when several recorded candidates match both ref and commit? — Picked: A — decide only after every recorded candidate has completed; the security pass is clean only when every matching run concluded `success` and opened no follow-up, and validation acts on the worst verdict among them. Alternatives: B — take the first matching candidate; C — stop at `Status: BLOCKED`. Why: B is the defect in the finding, and C stalls the project for a case the stage can settle safely. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-10-01] How is the issue's "re-audit on a mismatch" bounded? — Picked: A — reuse #5016's bound: one re-dispatch in the same cycle, and a second mismatch stops at `Status: BLOCKED`. Alternatives: B — count each re-dispatch as a new security cycle (cap 5); C — re-dispatch without a bound. Why: the project branch should not move during a run wait, so two mismatches in a row need a human, and C could loop (§28.C). Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-10-01] Should legacy mode, which audits the default branch, also require the audited commit to match? — Picked: A — no, legacy mode keeps the ref check. Alternatives: B — compare with the default branch head. Why: the default branch moves under almost every audit, so B would re-audit endlessly, and no project branch exists to pin. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine (`PR dispatch: #5841`, trig_01Fw4qrQgfcV7JiQApoQvLkM) in session session_01Nd8VfnooECN2NstKRn42NL, permission mode `auto`.
- Issue base `claude/implement-plan-issue-5016-dispatch-exact-run-id` (head `eb0413f`) is the head of open draft PR #5051 into `main`; no merged PR for it (checked 2026-10-01).
- Security pass: `security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`, so step 9 is skipped.
- Stale Routine sweep (2026-10-01): 100 listed, nothing to delete (33 kept, 67 not ours).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
