# Implement-Plan Log — Stop classifier-outage denials from stalling stage preflight and filing permission-prompt issues

- Plan: docs/plans/issue-4750-classifier-outage-get-session-plan.md
- Source issue: shubhodeep1/coding-workflows#4750
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4750-classifier-outage-get-session   Final PR: #4770 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: phase 1 blocked before start — protected paths need a Protected-path approval (asked on #4750, comment 5866547151)

## Phases
1. [ ] Phase 1 — outage-tolerant step 0 and outage-aware permission-prompt filing — protected paths: .claude/commands/implement-plan-claude.md, .claude/scripts/permission_prompts.py (and their workflow-templates/.claude/ mirrors)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should #4750 be fixed, given the call was already allowlisted and the reason was `Classifier unavailable`? — Picked: A — treat it as a classifier outage: remove the resume stage's self `get_session` from step 0, cap `get_session` retries at one, and stop filing outage denials. Alternatives: B — only the command-file change; C — only the filer change; D — add an allow rule (already present at `.claude/settings.json:104`). Why: the command change removes the ten observed denials; the filer change stops one outage from starting N Opus projects. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] What should the filer do with classifier-outage denials? — Picked: A — report them as a count under a new `outage_denials` key and never file them. Alternatives: B — one shared outage issue with a comment per outage; C — keep filing per pattern. Why: no repository change can fix an outage, and every filed issue is routed to a Claude project. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] How is an outage denial recognised? — Picked: A — a `PermissionDenied` record whose reason matches `classifier unavailable` (case-insensitive). Alternatives: B — any `PermissionDenied` whose reason mentions "classifier"; C — any `PermissionDenied`. Why: the exact text in all eight issues; anything wider could hide real gaps. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Which command files change? — Picked: A — only `/implement-plan-claude` step 0 and its template mirror, where every observed denial came from. Alternatives: B — also `implement-issue-claude.md` and `claude-issue-pickup.md`. Why: §5 minimal change set. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: started by `/implement-issue-claude` (session session_0153HZR3zNiEzJdUt3bfrVtz, dispatcher trigger trig_01MnhEevbsHWJSKe9KgXQRAJ).
- Security pass: run (`security_pass_skip.py`: no skip label).
- Sibling outage issues #4749, #4751, #4759–#4762, #4767 have the same root cause. They are left open for a human to decide (closing them is §23.C).
