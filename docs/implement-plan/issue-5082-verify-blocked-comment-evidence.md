# Implement-Plan Log — Unattended question guard counts only a verified blocked comment on the marker's issue

- Plan: docs/plans/issue-5082-verify-blocked-comment-evidence-plan.md
- Source issue: shubhodeep1/coding-workflows#5082 (https://github.com/shubhodeep1/coding-workflows/issues/5082)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5082-verify-blocked-comment-evidence   Final PR: #5088 draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: Q40 twin sync of `.claude/hooks/unattended_question_guard.py` on the phase 1 PR (hold claim posted)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (a blocked stop arms no wait)
- Last updated: 2026-09-29
- Last note: phase 1 built twin-first (Q40): the `workflow-templates/.claude/` twin, tests, and docs are on the phase 1 PR; the stage stopped BLOCKED with a twin-sync request on #5082.

## Phases
1. [ ] Phase 1 — verified blocked-comment evidence (MCP / `gh api` comment on the marker's repo#issue, body starts with the marker, result carries the comment URL, plus the `ai:claude-blocked` label write) — protected paths: `.claude/hooks/unattended_question_guard.py` (via its `workflow-templates/.claude/` twin)
   - [x] `workflow-templates/.claude/hooks/unattended_question_guard.py`: `_blocked_comment_targets`, `_blocked_label_targets`, `_gh_api_calls`, `_and_chain_segments`, `_result_text`, `_result_names_comment`, `blocked_comment_posted(turn, marker)`, `_instructions()` text
   - [x] `tests/test_unattended_question_guard.py`: updated positives, new negatives (echo, other issue/repo, marker not first, no label, no URL, compound command, `-F body=@file`, `--input`), `&&` chains and the `cd` prefix
   - [ ] `.claude/hooks/unattended_question_guard.py`: Q40 twin sync (the operator's approval window, Q62/Q64)
   - [x] CLAUDE.md §28.G (`workflow-templates/CLAUDE.md` is a symlink to it), `agents.md`, `README.md`, `changelog.d/5082-verify-blocked-comment-evidence.md`
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
- AD-7 [phase 1/1, 2026-09-29] AD-6 rejected the shape real sessions use (`cd <repo>; gh api …comments … && gh api …labels …`, including this project's own blocker). Which chains count? — Picked: A — `gh api` calls joined only by `&&`, after at most one leading `cd <plain path>` followed by `;` or `&&`. Alternatives: B — keep AD-6 as is; C — any `;`/`&&` chain of `gh api` or `cd` segments. Why: `&&` stops at the first failure and a plain `cd` can neither post nor print; B blocks every real blocker once, C lets a failed comment hide behind a later success. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A hook that inspects `Bash` commands in the transcript must accept the shapes sessions really use (`cd <repo>; …`, `&&` chains); test it against a real session transcript, not only hand-built commands. (files: .claude/hooks/unattended_question_guard.py, tests/test_unattended_question_guard.py)

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher (trigger `trig_012tNt3h3eR7REMU6pKD3dJr`) in session `session_01DUixJcgnR2rYT7ygq5Va1G`; permission mode auto.
- Base: `claude/implement-plan-issue-4911-unattended-question-guard` (the issue's `Integration branch:`); PR #4964 (its head) open, draft, into `main`, so the base has not moved.
- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Session tooling: the repo was attached mid-session, so the SessionStart hook had not run and `gh` was missing; the session ran `.claude/hooks/session-start.sh` to install it. No `mcp__github__*` tools in this session: GitHub writes use REST through the agent proxy.
- 2026-09-29 07:5xZ: phase 1 stopped before any code: it must edit `.claude/hooks/unattended_question_guard.py`, and #4948 / #4785 are still open, so the stage asked on #5082 (comment 5885870678).
- Protected-path approval: phase 1 — twin-first per Q40 (2026-09-29) (master session, #5082 comment 5886502707: "Q1: A"). The phase edits only the `workflow-templates/.claude/` twin, opens the phase PR into the project branch, posts a `hold` claim, and stops BLOCKED with a twin-sync request; the hook copy goes through the operator's approval window (Q62/Q64).
- 2026-09-29 08:4xZ: resumed in the same session by trigger `trig_01GXVCBWpT728TxkNitH7Qho`; `ai:claude-blocked` removed; the project branch was synced with its base (clean merge of `a54a4a3f`).
- Plan deviations: `test_hook_makes_no_network_calls` no longer forbids the literal `gh api` (the hook parses it as data) and forbids `os.system` / `popen` / `os.exec` instead; the target helpers return sets (`_blocked_comment_targets` / `_blocked_label_targets`) so one `&&` chain can carry the comment and the label (AD-7).
- Verification (2026-09-29, scratch copy with the twin synced into `.claude/`): `tests/test_unattended_question_guard.py` plus `test_gh_api_write_guard.py`, `test_pr_watch_guard.py`, `test_pr_check_in_reminder.py`, `test_update_workflows_guardrails.py`: 509 passed; `ruff check` clean. In the unsynced checkout only `test_template_parity[hooks/unattended_question_guard.py]` fails, as expected until the twin sync. Against this session's real transcript the new check accepts the #5082 blocker and rejects the same calls for #5083 and for another repo.
