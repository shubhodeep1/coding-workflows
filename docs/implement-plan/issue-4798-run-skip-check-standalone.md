# Implement-Plan Log — Run the security-pass skip check and the chain helpers as standalone Bash calls

- Plan: docs/completed/issue-4798-run-skip-check-standalone-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4798
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4798-run-skip-check-standalone   Final PR: #4810 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR (into the project branch)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01VUZBCfRW6VRQzfT7fLS4Za   safety net and hand-back: see the completion stage report
- Last updated: 2026-09-29
- Last note: Phase 1 merged, conformance CONFORMANT, security cycle 1 clean, validation cycle 1 pass; completion PR moves the plan to docs/completed/. Next: final-merge (mark final PR #4810 ready).

## Phases
1. [x] Phase 1 — standalone-call guidance for the skip check and the chain helpers   — protected paths: .claude/commands/implement-issue-claude.md, .claude/commands/implement-plan-claude.md   — PR #4823 merged 2026-09-28 into the project branch (0fd39cf); review rounds: 1; interventions: 0
   - `implement-issue-claude.md` step 6: run `security_pass_skip.py` as its own Bash call, exactly as written.
   - `implement-plan-claude.md` Helpers intro: run every helper and allowlisted script call as its own Bash call.
   - Byte-identical `workflow-templates/.claude/commands/` twins.
   - `tests/test_implement_issue_claude_command.py::test_allowlisted_calls_run_standalone`.
   - `changelog.d/4798-standalone-helper-calls.md`.
   - Done: both sentences in both copies; `tests/test_implement_issue_claude_command.py` passes.

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security). Concerns (HYPOTHESIS, not fixed): `fix-claude-pr.md` runs the same helpers without the standalone-call sentence; env-prefixed `check_in_status.py` calls may not match the allow rule.

## Security pass
- Cycle 1 — run 36506398751 2026-09-29 (ref: project branch, range 3c3e54e..7292383): clean (conclusion success; tracker #3576 findings=0 followups_created=0)

## Validation
- Cycle 1 — run 36506984883 2026-09-29 (target_ref: project branch, head 5445f21): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 272s)

## Completion
- Completion PR (this PR) — doc moved to docs/completed/issue-4798-run-skip-check-standalone-plan.md
- Final PR #4810 draft — marked ready at the final-merge stage

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should the prompt be removed? — Picked: A — tell sessions to run the allowlisted call alone (command-file guidance). Alternatives: B — add allow rules for `echo`, `grep`, `ls`, and redirects; C — close as not planned. Why: the call is already allowlisted; the prompt came from the chained extras, and the issue's fix order puts command-file changes first and never widens permissions for unprescribed shapes. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How wide should the guidance be? — Picked: A — the skip check in `/implement-issue-claude` step 6 plus one sentence in `/implement-plan-claude`'s Helpers intro covering every helper. Alternatives: B — only step 6; C — also CLAUDE.md §23.I and agents.md. Why: every helper has the same failure mode, and the Helpers intro is where the chain already sets shell-shape rules. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Is README.md or agents.md updated (§7)? — Picked: A — no; no env var, DB behaviour, or operational step changes. Alternatives: B — add a line to agents.md's §23.I helper section. Why: §5 minimal change set. Applied in: no code change. Status: pending review

## Lessons
- [source:plan-deviation] A twin-first phase breaks every parity test over the edited command files until `[claude-twin-sync]`, not only the one the plan names; grep `tests/` for each filename and list all of them in the hold. (files: tests/test_implement_issue_claude_command.py, tests/test_implement_plan_claude_command.py, tests/test_ingest_implement_plan_lessons.py)

## Notes
- 2026-09-28: issue mode; permission mode `auto` (session `session_015vMFAhzJ45YNs5dBPbv9mf`).
- 2026-09-28: `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`; security pass runs.
- 2026-09-28: phase 1 is marked protected paths; stopped before start and asked `Q1` on #4798. The operator's interim twin-first rule (#4750, Q40: A, until #4785 lands) is offered there as option D.
- Protected-path approval: phase 1 — D (twin-first, 2026-09-28)
- 2026-09-28: resumed on `/reclarify` in session `session_01QZU68PgrZ39yaqYs4BkRQM`; #4785 is still open, so the interim twin-first rule applies. Phase 1 → PR #4823 (twins, test, fragment only). Until the sync, 5 checks fail by design: `test_template_parity[implement-issue-claude.md]`, `test_template_parity[implement-plan-claude.md]`, `test_allowlisted_calls_run_standalone[live]` (tests/test_implement_issue_claude_command.py), `test_template_parity` (tests/test_implement_plan_claude_command.py), `test_implement_plan_claude_copies_are_identical` (tests/test_ingest_implement_plan_lessons.py). With the live path pointed at the twins, the contract suite passes (24 passed, repo-only pickup tests excluded).
- 2026-09-28: phase 1 twin sync (Q2: A) and operator merge (Q3: A) answered on #4798; PR #4823 merged into the project branch as 0fd39cf.
- 2026-09-29: stage-start syncs merged main into the project branch (7292383, 5445f21, 1746dd3).
