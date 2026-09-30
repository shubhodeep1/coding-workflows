# Implement-Plan Log — Require a merged final PR and its project log before a final-merge resume

- Plan: docs/plans/issue-5513-final-merge-resume-attestation-plan.md
- Source issue: shubhodeep1/coding-workflows#5513 (https://github.com/shubhodeep1/coding-workflows/issues/5513)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5513-final-merge-resume-attestation   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from claude/implement-plan-issue-5222-final-merge-resume-closed-issue; implementing phase 1

## Phases
1. [ ] Phase 1 — fail closed without a repository attestation   — not started; protected paths: .claude/commands/implement-issue-claude.md (twin-first)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; security_pass_skip.py)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where is the attestation enforced? — Picked: A — in the shared `final_merge_resume` rule, with both callers (clarify and the intake) fetching it through one helper. Alternatives: B — only in the intake's `authorize_target`; C — only in clarify. Why: the finding names the shared rule, and one rule keeps clarify from dispatching a forged block that the intake would then refuse. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] What attests a final-merge stage? — Picked: A — one default-branch progress log for the issue at a `final-merge` stage naming a same-repo final PR merged into the default branch, the blocked comment created before that merge, and a trusted `/reclarify` after it. Alternatives: B — the log and the merged PR only, with no time binding; C — only a merged PR found from the issue's close event. Why: a finished project's log can keep `Stage: final-merge`, so without the time binding a newly forged block on it would pass. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] Does `/implement-issue-claude` step 2 also list the attestation? — Picked: A — yes, edit the twin (twin-first, the phase PR holds for the sync). Alternatives: B — no, leave the command unchanged because the intake is the boundary. Why: §1 says the safer option wins, and step 2 claims to be "the same rule as `final_merge_resume`", which would be stale otherwise. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] How does an attestation read failure behave? — Picked: A — fail closed: clarify keeps the closed-issue skip with a warning, the intake rejects `authorization_read_failed`, and a 404 on the log directory counts as `no_project_log`. Alternatives: B — treat every read failure as ineligible (`issue_closed`). Why: this matches the intake's existing read-failure reason, so a transient error is distinguishable from a forged block. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] What if several logs match the issue? — Picked: A — fail closed (`ambiguous_project_log`), and also when more than 5 `issue-<N>-*.md` candidates exist. Alternatives: B — take the first log in name order. Why: §1; picking one silently could attest from the wrong project. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5513#issuecomment-5906453118
- Session: session_012xp91AMFqUWjdp3Jow6Nhw (invoking /implement-issue-claude session, Auto mode).
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
