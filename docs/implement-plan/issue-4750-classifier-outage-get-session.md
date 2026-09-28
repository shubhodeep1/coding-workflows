# Implement-Plan Log — Stop classifier-outage denials from stalling stage preflight and filing permission-prompt issues

- Plan: docs/plans/issue-4750-classifier-outage-get-session-plan.md
- Source issue: shubhodeep1/coding-workflows#4750
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4750-classifier-outage-get-session   Final PR: #4770 draft
- Status: BLOCKED
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4821 (phase 1, review round 1 fix) — twin sync by the supervising session, then /reclarify
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (project checker session_01SDx9eEYSDcok29Jq6jwD3m kept idle for reuse)
- Last updated: 2026-09-28
- Last note: review round 1 (head e740614) fixed twin-first in PR #4821: report-line template, classifier regex anchor, single log load, step-0 fallback wording; `total` and step-0 reminder findings rejected with reasons. Hold claim posted; BLOCKED until the supervising session copies the two workflow-templates/.claude twins into .claude/ as [claude-twin-sync] and comments /reclarify. On resume, arm the wait on PR #4821 — do not re-fix round 1.

## Phases
1. [ ] Phase 1 — classifier-outage handling: filer split, CLAUDE.md §23.J retry rule, and outage-tolerant step 0 — protected paths: .claude/commands/implement-plan-claude.md, .claude/scripts/permission_prompts.py (edited only in their workflow-templates/.claude/ twins, Q40) — PR #4821 open (hold: awaiting [claude-twin-sync] of the round-1 fix); review rounds: 1; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should #4750 be fixed, given the call was already allowlisted and the reason was `Classifier unavailable`? — Picked: A — treat it as a classifier outage: remove the resume stage's self `get_session` from step 0, cap `get_session` retries at one, and stop filing outage denials. Alternatives: B — only the command-file change; C — only the filer change; D — add an allow rule (already present at `.claude/settings.json:104`). Why: the command change removes the ten observed denials; the filer change stops one outage from starting N Opus projects. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] What should the filer do with classifier-outage denials? — Picked: A — report them as a count under a new `outage_denials` key and never file them. Alternatives: B — one shared outage issue with a comment per outage; C — keep filing per pattern. Why: no repository change can fix an outage, and every filed issue is routed to a Claude project. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] How is an outage denial recognised? — Picked: A — a `PermissionDenied` record whose reason matches `classifier unavailable` (case-insensitive). Alternatives: B — any `PermissionDenied` whose reason mentions "classifier"; C — any `PermissionDenied`. Why: the exact text in all eight issues; anything wider could hide real gaps. Applied in: phase 1 PR. Status: superseded by AD-5 (operator Q39 item 1 names no-verdict or outage errors, wider than A)
- AD-4 [plan, 2026-09-28] Which command files change? — Picked: A — only `/implement-plan-claude` step 0 and its template mirror, where every observed denial came from. Alternatives: B — also `implement-issue-claude.md` and `claude-issue-pickup.md`. Why: §5 minimal change set. Applied in: phase 1 PR. Status: pending review
- AD-5 [phase 1/1, 2026-09-28] Which reasons count as a classifier no-verdict or outage error (Q39 item 1)? — Picked: A — `PermissionDenied` only, reason matching `classifier` followed by `unavailable`, `error`, `timed out`, `timeout`, or `overloaded` (optional `is`), or a no-verdict phrase (`no verdict`, `without a verdict`, `did not return a verdict`, `could not reach a verdict`). Alternatives: B — only `classifier unavailable` (AD-3); C — any `PermissionDenied` that mentions "classifier". Why: covers the wording Q39 names while a real block, which carries the classifier's reason, still files. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-28] Where does the retry-then-wait rule (Q39 item 2) live? — Picked: A — new CLAUDE.md §23.J next to §23.I, plus `/implement-plan-claude` step 0 for its `get_session` reads. Alternatives: B — a new top-level §29; C — every command file that calls a tool. Why: §23.I already governs permission prompts, CLAUDE.md reaches every interactive session, and nothing is renumbered (§6). Applied in: phase 1 PR. Status: pending review
- AD-7 [phase 1/1, 2026-09-28] How long does a session wait after a second refusal, and is there a cap? — Picked: A — one `send_later` with `delay_minutes: 30` per refusal, no cap; if `send_later` is refused too, end the turn and name the refused step. Alternatives: B — back off 30/60/120 minutes; C — stop BLOCKED after the fourth outage wake. Why: Q39 item 2 says about 30 minutes and never BLOCKED; C contradicts it and B adds state a stage does not keep. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:intervention] When a report-template line gains an optional clause, use the file's `[...]` optional-suffix convention and test the whole line, not just the new substring: a substring test passed while the line read `<error>><; …>`. (files: .claude/commands/implement-plan-claude.md, tests/test_permission_prompts.py)
- [source:intervention] A reason regex that exempts records from filing must be anchored on the subsystem's own word (here `classifier`); a bare phrase like `did not return a verdict` would silently hide real denials from another source. (files: .claude/scripts/permission_prompts.py)

## Notes
- Issue mode: started by `/implement-issue-claude` (session session_0153HZR3zNiEzJdUt3bfrVtz, dispatcher trigger trig_01MnhEevbsHWJSKe9KgXQRAJ).
- Security pass: run (`security_pass_skip.py`: no skip label).
- Sibling outage issues #4749, #4751, #4759–#4762, #4767 have the same root cause (plan stage); the operator later closed them as duplicates of #4750 (see Q39 below).
- Protected-path approval: phase 1 — A (2026-09-28), as the interim twin-first rule (Q40 A) until #4785 lands: `.claude/**` changes are made only in their `workflow-templates/.claude/**` twins; the supervising session copies them as `[claude-twin-sync]` (operator comment 5868311173).
- Operator Q39 A (2026-09-28): #4750 is the single fix for the classifier-outage cluster; #4749, #4751, #4759, #4760, #4761, #4762, #4767, #4779, #4780 closed as duplicates by the operator. #4808 (same reason) was filed after Q39 and is still open; closing it is the operator's call (§23.C).
- Local test run: python3 here is 3.11, so `tests/test_workflow_retro.py` cannot be collected (an f-string with a backslash in `scripts/workflow_retro.py`, fine on CI's newer Python); unrelated to this project.
- Review round 1 (2026-09-28, session session_01VpEWZrNVBtCNysdGtCgmJV): project branch already up to date with main (f92848d); fixes pushed twin-only per Q40 A.
