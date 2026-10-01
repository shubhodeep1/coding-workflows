# Implement-Plan Log — Keep session data in permission-prompt issues inside code spans it cannot close

- Plan: docs/plans/issue-5810-escape-class-comment-pattern-plan.md
- Source issue: shubhodeep1/coding-workflows#5810 (https://github.com/shubhodeep1/coding-workflows/issues/5810)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates
- Project branch: claude/implement-plan-issue-5810-escape-class-comment-pattern   Final PR: #5833 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5855 (review of the round 2 fix)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Msc3b8xfUvDG34kWXoXhaV   safety net and hand-back re-armed by the review round 2 stage after its push (ids in its report)
- Last updated: 2026-10-01
- Last note: twin sync 2 landed (bbaacdc), so the twin-sync hold is lifted. Review round 2 (workflow round 1 on bbaacdc): the stale-log finding is fixed in this log; the `comment_body` wording finding is rejected (a11834a already says which values each path renders). No `.claude/` change, so no twin sync is needed.

## Phases
1. [ ] Phase 1 — code spans for session data and the spec rule   — protected paths: `.claude/scripts/permission_prompts.py`, `.claude/commands/implement-issue-claude.md` (edited through their `workflow-templates/.claude/` twins)
   - `permission_prompts.py` twin: `_markdown_code_span`; shape on both `**Pattern:**` lines, tool name, and reasons rendered through it; module docstring
   - `/implement-issue-claude` twin: step 1 evidence-not-spec rule
   - `tests/test_permission_prompt_duplicates.py`: new cases (fail against the base twin), updated reason assertion
   - `CLAUDE.md` §23.I and `agents.md`: "Issue text is untrusted data" sentences; `changelog.d/5810-permission-prompt-code-spans.md` [new]
   - Done: goals 1–4 hold; new tests pass; with the twins copied, `tests/test_permission_prompts.py`, `tests/test_permission_prompt_duplicates.py`, `tests/test_implement_issue_claude_command.py` pass; ruff clean
   - PR #5855 open (waiting on review); review rounds: 2 (c83626e → a11834a; bbaacdc → this log fix); twin syncs: 2 (c83626e, bbaacdc, 2026-10-01); interventions: 0

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
- [source:intervention] When a spec rule lists which generated fields are evidence, name every session-derived field the renderer writes (the tool name as well as the shape), and describe per rendering path which fields it shows, so the docs never claim a path renders a field it does not. (files: workflow-templates/.claude/commands/implement-issue-claude.md, workflow-templates/.claude/scripts/permission_prompts.py)
- [source:intervention] A `[claude-twin-sync]` commit that lifts a twin-sync block must also update the progress log (`Status`, `Waiting on`, `Check-in`, the phase's twin-sync count) in the same commit; otherwise the next review round flags a log that still says `BLOCKED` on a sync already done. (files: docs/implement-plan/issue-5810-escape-class-comment-pattern.md)

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-10-01)
- Security pass: `security_pass_skip.py` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
