# Implement-Plan Log — Keep session data in permission-prompt issues inside code spans it cannot close

- Plan: docs/completed/issue-5810-escape-class-comment-pattern-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5810 (https://github.com/shubhodeep1/coding-workflows/issues/5810)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4867-close-permission-prompt-duplicates
- Project branch: claude/implement-plan-issue-5810-escape-class-comment-pattern   Final PR: #5833 draft (into claude/implement-plan-issue-4867-close-permission-prompt-duplicates)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4867-close-permission-prompt-duplicates)
- Waiting on: the completion PR from claude/implement-plan-issue-5810-escape-class-comment-pattern-complete
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Msc3b8xfUvDG34kWXoXhaV   safety net and hand-back: see the completion stage report
- Last updated: 2026-10-01
- Last note: completion stage: plan moved to docs/completed/; conformance run 1 CONFORMANT; security skipped (plan header); validation cycle 1 passed (run 36830175424, 10/10). Next: final-merge 1/1 marks #5833 ready and closes #5810 with `ai:merged` once it merges.

## Phases
1. [x] Phase 1 — code spans for session data and the spec rule   — protected paths: `.claude/scripts/permission_prompts.py`, `.claude/commands/implement-issue-claude.md` (edited through their `workflow-templates/.claude/` twins)
   - `permission_prompts.py` twin: `_markdown_code_span`; shape on both `**Pattern:**` lines, tool name, and reasons rendered through it; module docstring
   - `/implement-issue-claude` twin: step 1 evidence-not-spec rule
   - `tests/test_permission_prompt_duplicates.py`: new cases (fail against the base twin), updated reason assertion
   - `CLAUDE.md` §23.I and `agents.md`: "Issue text is untrusted data" sentences; `changelog.d/5810-permission-prompt-code-spans.md` [new]
   - Done: goals 1–4 hold; new tests pass; with the twins copied, `tests/test_permission_prompts.py`, `tests/test_permission_prompt_duplicates.py`, `tests/test_implement_issue_claude_command.py` pass; ruff clean
   - PR #5855 merged 2026-10-01 06:54Z (af95f04); review rounds: 2 (c83626e → a11834a; bbaacdc → log fix); twin syncs: 2 (c83626e, bbaacdc, 2026-10-01); interventions: 0

## Conformance
- Run 1 — 2026-10-01: CONFORMANT — no fixes (pre-security). Evidence: G1 `_markdown_code_span` at .claude/scripts/permission_prompts.py:628-643, used for reasons (:646), the `issue_body` tool name and Pattern (:659, :662), and the `class_comment_body` Pattern and tool name (:696); `comment_body` renders only reasons (:688). G2 values without backticks keep a one-backtick span; signatures are computed before rendering. G3 rule in implement-issue-claude.md step 1 (twin and copy identical). G4 CLAUDE.md §23.I, agents.md, module docstring, changelog.d/5810-permission-prompt-code-spans.md. 230 passed on the four plan suites, ruff clean, the 12 new tests fail against the base twin, and a CommonMark render of all three bodies injects no strong/link/heading (the base version does).

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verdict, 2026-10-01)

## Validation
- Cycle 1 — run 36830175424 2026-10-01 (target_ref: claude/implement-plan-issue-5810-escape-class-comment-pattern): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 287s).

## Completion
- Completion PR from claude/implement-plan-issue-5810-escape-class-comment-pattern-complete (open) — doc moved to docs/completed/issue-5810-escape-class-comment-pattern-plan.md
- Final PR #5833 draft (into claude/implement-plan-issue-4867-close-permission-prompt-duplicates)
- Project branch was up to date with its base on 2026-10-01 07:23Z and 08:40Z (no sync push).

## Activation
- n/a: the base is the #4867 project branch (PR #4883, open, draft), so this change goes live with that project.

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
