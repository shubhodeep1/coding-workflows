# Implement-Plan Log — Keep automation text from resuming a blocked issue through a later-line /reclarify

- Plan: docs/plans/issue-5309-reclarify-fail-closed-plan.md
- Source issue: shubhodeep1/coding-workflows#5309
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5309-reclarify-fail-closed   Final PR: #5324 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance-fix PR (this PR, run 1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_013xGq4H3mHhMuXa8UkuLynY   safety net / hand-back: named in the stage report and the next `— resume.` block
- Last updated: 2026-09-30
- Last note: conformance run 1 CONFORMANT (Correctness: CONCERNS); one EVIDENCE-BASED concern fixed in this PR: the `clarify.yml` blocked comment that adds `ai:blocked` now ends with `<!-- ai:clarify-blocked:v1 -->`.

## Phases
1. [x] Phase 1 — fail-closed later-line `/reclarify` gate   — PR #5439 merged 2026-09-30 (merge commit 2e8e39e); review rounds: 0; interventions: 0
   - Job predicate (four copies): `<!--` exclusion, `ai:orchestrator-tracking` / `ai:orchestrator-managed` exclusion.
   - `Decide clarify route`: same rules plus fenced-code-block scan; `RELEASE_CLAUDE_CLAIM` also for stale Claude labels.
   - Release step drops `ai:claude-blocked` / `ai:claude-handoff-failed`.
   - Markers: `ai:clarify-escalation:v1`, `ai:plan-blocked:v1`, `ai:implement-blocked:v1`, `ai:clarification-required:v1`.
   - Tests: predicate contract, route-step gate, marker sites, release step.
   - Docs: README, agents.md, docs/how-it-works.md, `changelog.d/5243-reclarify-any-line.md`, `changelog.d/5309-reclarify-fail-closed.md`.
   - Done: four predicates match; route-step cases pass; markers present; named suites green.

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Correctness: CONCERNS) — fix PR: conformance-fix 1 (this PR; `claude/implement-plan-issue-5309-reclarify-fail-closed-conformance-fix-1`) (pre-security). Finding: [CONCERN] `.github/workflows/clarify.yml` "Handle blocked clarification output" adds `ai:blocked` and posted no `<!-- ai:…:v1 -->` marker, against the plan's goal that every comment adding `ai:blocked` carries one and the README / changelog statement that the clarify comment does. Not exploitable today (its reason is one line after `Reason: `), so defence in depth.

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
- [source:conformance] When a plan says "every comment that adds label X gets a marker", grep every `--add-label` / `labels[]=` writer of X across `.github/workflows/` and `scripts/` rather than trusting the plan's Context list; the Context list for #5309 missed `clarify.yml`'s own blocked comment. (files: .github/workflows/clarify.yml)

## Notes
- 2026-09-30: phase 1 PR #5439 opened and merged (2e8e39e); first checker session_013xGq4H3mHhMuXa8UkuLynY (safety net trig_01TZE49qmMhmjbBodQWrXBDn and hand-back trig_013sco7FhEAfBSrFzZTxJFJf deleted by the conformance stage); issue progress comment 5901804455 on #5309.
- Issue mode: plan written by `/implement-issue-claude` (session session_01Wb7AYbAih3CAQrNMqFVqLh); base branch `claude/implement-plan-issue-5243-reclarify-any-line` (project #5243, final PR #5266); security pass skipped (`security_pass_skip.py`: ai:security automation-produced issue).
