# Implement-Plan Log — Keep automation text from resuming a blocked issue through a later-line /reclarify

- Plan: docs/plans/issue-5309-reclarify-fail-closed-plan.md
- Source issue: shubhodeep1/coding-workflows#5309
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5309-reclarify-fail-closed   Final PR: #5324 draft
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: phase 1 PR (review workflow)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: phase 1 implemented: `<!--` + orchestrator-scope later-line gate, fenced-block route-step scan, four escalation markers, stale Claude labels released on a Codex route.

## Phases
1. [ ] Phase 1 — fail-closed later-line `/reclarify` gate   — PR open (waiting); review rounds: 0; interventions: 0
   - Job predicate (four copies): `<!--` exclusion, `ai:orchestrator-tracking` / `ai:orchestrator-managed` exclusion.
   - `Decide clarify route`: same rules plus fenced-code-block scan; `RELEASE_CLAUDE_CLAIM` also for stale Claude labels.
   - Release step drops `ai:claude-blocked` / `ai:claude-handoff-failed`.
   - Markers: `ai:clarify-escalation:v1`, `ai:plan-blocked:v1`, `ai:implement-blocked:v1`, `ai:clarification-required:v1`.
   - Tests: predicate contract, route-step gate, marker sites, release step.
   - Docs: README, agents.md, docs/how-it-works.md, `changelog.d/5243-reclarify-any-line.md`, `changelog.d/5309-reclarify-fail-closed.md`.
   - Done: four predicates match; route-step cases pass; markers present; named suites green.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should the reported path (an orchestrator escalation adds `ai:blocked`, then posts unmarked model text) be closed? — Picked: A — markers on every comment that adds `ai:blocked`, plus a later-line form that never applies on `ai:orchestrator-managed` / `ai:orchestrator-tracking` issues. Alternatives: B — escalation markers only; C — revert the later-line form. Why: B leaves unmarked poller and judge comments on orchestrator issues open; C undoes #5243. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Which comments count as automation for the later-line form? — Picked: A — any comment containing `<!--`. Alternatives: B — keep `<!-- ai:` and add `workflow-failure-heal:`. Why: fails closed for every present and future marker (§1). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-30] How should fenced model text (permission-prompt "Seen again" example) be kept from counting? — Picked: A — the route step ignores `/reclarify` lines inside fenced code blocks. Alternatives: B — marker in `.claude/scripts/permission_prompts.py` twin-first; C — accept as residual. Why: covers every fenced untrusted excerpt with no protected-path edit (§5). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] Should a switch to Codex clear `ai:claude-blocked` / `ai:claude-handoff-failed`? — Picked: A — yes, in the release step. Alternatives: B — leave them. Why: stale labels keep the later-line form open under Codex model text. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-30] Mark the non-orchestrated `plan.yml` "Clarification required" comment? — Picked: A — yes, `<!-- ai:clarification-required:v1 -->` as its last line. Alternatives: B — no. Why: it copies raw model output; the `^Clarification required` lookup is unchanged. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-30] Correct the unreleased `changelog.d/5243-reclarify-any-line.md`? — Picked: A — yes, in place, plus a new `5309` fragment. Alternatives: B — new fragment only. Why: the 5243 fragment would describe a rule that no longer holds (§12.B). Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode: plan written by `/implement-issue-claude` (session session_01Wb7AYbAih3CAQrNMqFVqLh); base branch `claude/implement-plan-issue-5243-reclarify-any-line` (project #5243, final PR #5266); security pass skipped (`security_pass_skip.py`: ai:security automation-produced issue).
