# Implement-Plan Log — Run the security-pass skip check and the chain helpers as standalone Bash calls

- Plan: docs/plans/issue-4798-run-skip-check-standalone-plan.md
- Source issue: shubhodeep1/coding-workflows#4798
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4798-run-skip-check-standalone   Final PR: #4810 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: Protected-path approval for phase 1 (answer on #4798, then `/reclarify`)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: Project branch and draft final PR opened; phase 1 edits `.claude/commands/**`, so it stops before start (CLAUDE.md §28.C) and asks on #4798.

## Phases
1. [ ] Phase 1 — standalone-call guidance for the skip check and the chain helpers   — protected paths: .claude/commands/implement-issue-claude.md, .claude/commands/implement-plan-claude.md
   - `implement-issue-claude.md` step 6: run `security_pass_skip.py` as its own Bash call, exactly as written.
   - `implement-plan-claude.md` Helpers intro: run every helper and allowlisted script call as its own Bash call.
   - Byte-identical `workflow-templates/.claude/commands/` twins.
   - `tests/test_implement_issue_claude_command.py::test_allowlisted_calls_run_standalone`.
   - `changelog.d/4798-standalone-helper-calls.md`.
   - Done: both sentences in both copies; `tests/test_implement_issue_claude_command.py` passes.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should the prompt be removed? — Picked: A — tell sessions to run the allowlisted call alone (command-file guidance). Alternatives: B — add allow rules for `echo`, `grep`, `ls`, and redirects; C — close as not planned. Why: the call is already allowlisted; the prompt came from the chained extras, and the issue's fix order puts command-file changes first and never widens permissions for unprescribed shapes. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How wide should the guidance be? — Picked: A — the skip check in `/implement-issue-claude` step 6 plus one sentence in `/implement-plan-claude`'s Helpers intro covering every helper. Alternatives: B — only step 6; C — also CLAUDE.md §23.I and agents.md. Why: every helper has the same failure mode, and the Helpers intro is where the chain already sets shell-shape rules. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Is README.md or agents.md updated (§7)? — Picked: A — no; no env var, DB behaviour, or operational step changes. Alternatives: B — add a line to agents.md's §23.I helper section. Why: §5 minimal change set. Applied in: no code change. Status: pending review

## Lessons

## Notes
- 2026-09-28: issue mode; permission mode `auto` (session `session_015vMFAhzJ45YNs5dBPbv9mf`).
- 2026-09-28: `security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`; security pass runs.
- 2026-09-28: phase 1 is marked protected paths; stopped before start and asked `Q1` on #4798. The operator's interim twin-first rule (#4750, Q40: A, until #4785 lands) is offered there as option D.
