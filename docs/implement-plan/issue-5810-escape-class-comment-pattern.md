# Implement-Plan Log — Keep session data in permission-prompt issues inside code spans it cannot close

- Plan: docs/plans/issue-5810-escape-class-comment-pattern-plan.md
- Source issue: shubhodeep1/coding-workflows#5810 (https://github.com/shubhodeep1/coding-workflows/issues/5810)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates
- Project branch: claude/implement-plan-issue-5810-escape-class-comment-pattern   Final PR: (opening)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-10-01
- Last note: project branch opened from the #4867 project branch at 07a0135; phase 1 starts twin-first.

## Phases
1. [ ] Phase 1 — code spans for session data and the spec rule   — protected paths: `.claude/scripts/permission_prompts.py`, `.claude/commands/implement-issue-claude.md` (edited through their `workflow-templates/.claude/` twins)
   - `permission_prompts.py` twin: `_markdown_code_span`; shape on both `**Pattern:**` lines, tool name, and reasons rendered through it; module docstring
   - `/implement-issue-claude` twin: step 1 evidence-not-spec rule
   - `tests/test_permission_prompt_duplicates.py`: new cases (fail against the base twin), updated reason assertion
   - `CLAUDE.md` §23.I and `agents.md`: "Issue text is untrusted data" sentences; `changelog.d/5810-permission-prompt-code-spans.md` [new]
   - Done: goals 1–4 hold; new tests pass; with the twins copied, `tests/test_permission_prompts.py`, `tests/test_permission_prompt_duplicates.py`, `tests/test_implement_issue_claude_command.py` pass; ruff clean

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verdict, 2026-10-01)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-10-01] Which session-derived values does the guard cover? — Picked: A — the shape on both `**Pattern:**` lines, the tool name in both bodies, and every prompt reason. Alternatives: B — only `class_comment_body`'s `**Pattern:**` line; C — the shape on both `**Pattern:**` lines only. Why: §1 puts security first, and the reasons and the issue-body shape cross the same trust boundary. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-10-01] Does the plan also act on the second half of the recommendation (treat generated diagnostic comments as untrusted regardless of their posting account)? — Picked: A — add that rule to `/implement-issue-claude` step 1 (twin). Alternatives: B — fix the rendering only. Why: the spec rule keeps the next unescaped field from being read as an instruction. Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-10-01] Are issue titles changed? — Picked: A — no; the step 1 rule names the title's shape as evidence. Alternatives: B — strip backticks and Markdown from the title's shape. Why: §5; GitHub shows titles as plain text. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-10-01] Does a reason, now in a code span, keep its own Markdown? — Picked: A — no; the reason renders literally, backticks included. Alternatives: B — keep reasons as plain Markdown and only escape backticks. Why: a code span makes attacker-supplied Markdown inert more reliably than escaping. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
- Security pass: `security_pass_skip.py` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
