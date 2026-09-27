# Implement-Plan Log — Claude issue intake authorizes the target issue and its dispatcher

- Plan: docs/plans/issue-4620-intake-authorize-target-issue-plan.md
- Source issue: shubhodeep1/coding-workflows#4620
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4620-intake-authorize-target-issue   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-27
- Last note: project branch opened by session_01NBLHb1ZnPwLW1njQTFnP7D

## Phases
1. [ ] Phase 1 — intake authorizes the target issue and dispatcher

## Conformance

## Security pass
- Skipped: plan header `Security pass: skip (ai:security: automation-produced issue)`

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-27] How should each dispatch be bound to an authorized party? — Picked: A — check that every dispatcher login GitHub reports for the run (`github.actor`, and `github.triggering_actor` when different) has `admin` or `write` permission on the target repo. Alternatives: B — verify the payload's `reporter_run_url` is a live clarify run in the target repo; C — both. Why: the actor is set by GitHub and cannot be forged in the payload, while a run URL only proves some run exists and cannot be tied to one issue. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-27] Which issue authors may be implemented? — Picked: A — clarify's rule: a `User` with `OWNER`/`MEMBER`/`COLLABORATOR` association or `github-actions[bot]`, or any author when a trusted `User` commented `/reclarify`. Alternatives: B — trusted authors only, ignoring `/reclarify`; C — no author check. Why: matches the gate the dispatch bypasses, so a collaborator can still vouch for an outside issue as clarify allows today. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-27] What does a refused or unverifiable dispatch write? — Picked: A — nothing on the target issue; `CLAUDE_ISSUE_INTAKE rejected` log, `::error::`, Telegram ERROR, exit 1, also for read failures. Alternatives: B — the existing `fail` path (label + comment on the target); C — B for read failures only. Why: until authorization succeeds the target is unverified, so an unauthorized dispatcher must not be able to make `GH_PAT` write to arbitrary issues. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-27] Should the intake queue a closed issue? — Picked: A — refuse as `issue_closed`. Alternatives: B — queue it and let `/implement-issue-claude` stop. Why: the recommendation asks to verify the live target, clarify skips closed issues, and a refused closed issue costs no session. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-27] Add a second author gate inside `/implement-issue-claude`? — Picked: A — no; the intake is the only producer of queue items the pickup trusts (`github-actions[bot]`), so one gate there closes the path. Alternatives: B — also gate in the command file. Why: §5 minimal change; a human running the command by hand chooses the issue themselves, and the command file ships to every consumer repo. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/4620#issuecomment-5854273112
