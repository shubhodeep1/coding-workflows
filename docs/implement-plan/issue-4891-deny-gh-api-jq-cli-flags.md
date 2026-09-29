# Implement-Plan Log — gh api guard: deny jq command-line options passed to `--jq` instead of prompting

- Plan: docs/plans/issue-4891-deny-gh-api-jq-cli-flags-plan.md
- Source issue: shubhodeep1/coding-workflows#4891 (https://github.com/shubhodeep1/coding-workflows/issues/4891)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4891-deny-gh-api-jq-cli-flags   Final PR: (opened after this commit) draft
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: an operator answer to the protected-path question on #4891, then `/reclarify`
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (blocked before phase 1 started; no PR to wait on)
- Last updated: 2026-09-29
- Last note: plan written; phase 1 edits `.claude/hooks/gh_api_write_guard.py` (protected path) and the log has no `Protected-path approval: phase 1` line, so the phase stops before it starts (CLAUDE.md §28.C)

## Phases
1. [ ] Phase 1 — deny jq CLI options passed to `--jq` in the gh api guard (hook + twin, tests, CLAUDE.md §23.H, agents.md, changelog)   — protected paths: `.claude/hooks/gh_api_write_guard.py`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should the prompt on a malformed `gh api --jq` call be fixed? — Picked: A — the guard returns `deny` with a corrective reason for a `--jq`/`-q` value that is a jq command-line option. Alternatives: B — deny every unreadable call; C — CLAUDE.md §23.D guidance only. Why: deterministic, narrower than today, and fixes the observed shape; B would deny valid calls whose word count depends on shell expansion; C is ignored under load (#4858). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Which `--jq` values count as jq options? — Picked: A — values matching `^--?[A-Za-z]`. Alternatives: B — any value starting with `-`; C — an explicit list of jq's options. Why: catches every jq option, current and future, without denying valid programs such as `-.size` or `-1`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How does the deny combine with other calls in the same Bash command? — Picked: A — deny the whole call; it wins over ask and allow, after the unchanged hidden-call and parse-failure asks. Alternatives: B — keep ask and only improve the reason text. Why: a hook decides once per tool call, a deny runs nothing, and B still stalls unattended sessions. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Add an environment-variable kill switch for the deny? — Picked: A — no. Alternatives: B — `CLAUDE_GH_API_GUARD_DENY=off`. Why: §23.H states the guard deliberately has no escape hatch, and the deny only narrows behaviour. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] Do the permission-prompt logger or filer need changes? — Picked: A — no, unless the phase finds from the Claude Code hooks documentation that a `PreToolUse` deny raises `PermissionRequest` or `PermissionDenied`; then `permission_prompts.py` skips records whose reason starts with the guard's deny prefix, and the phase records a plan deviation. Alternatives: B — add the filer skip now. Why: §5. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Is #4891 a duplicate of an open fix issue? — Picked: A — no; implement it. Alternatives: B — treat it as a duplicate of #4912. Why: #4912 is a newer issue with a different cause (the hidden-call rule), and no open issue fixes malformed `--jq` calls. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Started by the Claude issue dispatcher routine in session session_01GmnPyCgwEwn3qbbRKYFMfY (Auto mode).
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Blocked before phase 1: protected path `.claude/hooks/gh_api_write_guard.py`; the question is on #4891 (`ai:claude-blocked`).
