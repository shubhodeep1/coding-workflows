# Implement-Plan Log — Unattended question guard counts only a verified blocked comment on the marker's issue

- Plan: docs/plans/issue-5082-verify-blocked-comment-evidence-plan.md
- Source issue: shubhodeep1/coding-workflows#5082 (https://github.com/shubhodeep1/coding-workflows/issues/5082)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5082-verify-blocked-comment-evidence   Final PR: pending (opened after this commit)
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none (protected-path approval for phase 1 on #5082)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (a blocked stop arms no wait)
- Last updated: 2026-09-29
- Last note: phase 1 must edit `.claude/hooks/unattended_question_guard.py`, a protected path (CLAUDE.md §28.C); stopped before writing any code and asked on #5082 how to run it.

## Phases
1. [ ] Phase 1 — verified blocked-comment evidence (MCP / single `gh api` comment on the marker's repo#issue, body starts with the marker, result carries the comment URL, plus the `ai:claude-blocked` label write) — protected paths: `.claude/hooks/unattended_question_guard.py` (via its `workflow-templates/.claude/` twin)
   - [ ] `workflow-templates/.claude/hooks/unattended_question_guard.py`: `_blocked_comment_target`, `_blocked_label_target`, `_result_text`, `blocked_comment_posted(turn, marker)`, `_instructions()` text
   - [ ] `tests/test_unattended_question_guard.py`: updated positives, new negatives (echo, other issue/repo, marker not first, no label, no URL, compound command, `-F body=@file`, `--input`)
   - [ ] `.claude/hooks/unattended_question_guard.py`: Q40 twin sync
   - [ ] CLAUDE.md + `workflow-templates/CLAUDE.md` §28.G, `agents.md`, `README.md`, `changelog.d/5082-verify-blocked-comment-evidence.md`
   - Done: every plan Goal has a passing test with the twin synced; related hook suites still pass

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which tool calls can count as posting the blocked comment? — Picked: A — `mcp__*__add_issue_comment` and exactly one simple `gh api` POST to `repos/<repo>/issues/<N>/comments` in `Bash`. Alternatives: B — MCP only; C — any tool whose input names the repo, issue, and marker. Why: both forms are used by real sessions; C keeps the spoofing hole. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] How is "verified" established without API calls? — Picked: A — a non-error `tool_result` whose text contains that issue's comment `html_url` or `issue_url`. Alternatives: B — a non-error result only; C — read the comment back through the API. Why: B proves nothing, and C breaks the hook's no-API contract (§15). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Is the `ai:claude-blocked` label write required too? — Picked: A — yes, a successful same-turn label write on the same issue. Alternatives: B — the comment alone. Why: the issue's recommendation and §28.C require both. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] How are the repo and issue matched? — Picked: A — `owner/repo` case-insensitive, issue number exact. Alternatives: B — exact-case repo. Why: GitHub names are case-insensitive. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Where must the marker appear in the body? — Picked: A — at the start, after leading whitespace. Alternatives: B — anywhere. Why: §28.C says the comment starts with it. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] What `Bash` command shape counts? — Picked: A — exactly one simple `gh api` command, no `--input`, no `-F body=@file`. Alternatives: B — the first `gh api` anywhere in the command. Why: B lets an `echo` or a second command wrap the call. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `trig_012tNt3h3eR7REMU6pKD3dJr`) in session `session_01DUixJcgnR2rYT7ygq5Va1G`; permission mode auto.
- Base: `claude/implement-plan-issue-4911-unattended-question-guard` (the issue's `Integration branch:`); PR #4964 (its head) open, draft, into `main`, so the base has not moved.
- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Session tooling: the repo was attached mid-session, so the SessionStart hook had not run and `gh` was missing; the session ran `.claude/hooks/session-start.sh` to install it. No `mcp__github__*` tools in this session: GitHub writes use REST through the agent proxy.
- Protected-path approval: phase 1 — not recorded yet. #4948 (automatic twin-first default) and #4785 (Actions `.claude/` sync) are still open, so the stage asked on #5082 (standing operator decision Q40, `docs/operations/master-session.md`, is the recommended answer).
