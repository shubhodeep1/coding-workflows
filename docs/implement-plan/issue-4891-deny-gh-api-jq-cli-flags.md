# Implement-Plan Log — gh api guard: deny jq command-line options passed to `--jq` instead of prompting

- Plan: docs/completed/issue-4891-deny-gh-api-jq-cli-flags-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4891 (https://github.com/shubhodeep1/coding-workflows/issues/4891)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4891-deny-gh-api-jq-cli-flags   Final PR: #4920 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: pending verify-activation
- Waiting on: completion PR (this log commit) into the project branch
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01EsP7JrmP63aCBSWP9M5S1d (project checker, reused)   safety net and hand-back: in the validation 1/3 — read result stage report
- Last updated: 2026-09-29
- Last note: validation cycle 1 (run 36638738895, project branch at fadfddf) passed 10/10; project branch synced with `main` (2a9a73e); completion PR opened to move the plan to docs/completed/

## Phases
1. [x] Phase 1 — deny jq CLI options passed to `--jq` in the gh api guard (hook + twin, tests, CLAUDE.md §23.H, agents.md, changelog)   — protected paths: `.claude/hooks/gh_api_write_guard.py` — PR #5033 merged 2026-09-29 (c64c029); `[claude-twin-sync]` df89ef4 copied the twin into `.claude/`; review rounds: 0 findings rounds (1 `[claude-merge-resolve]` conflict merge); interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security)

## Security pass
- Cycle 1 — run 36637716889 2026-09-29 (ref: claude/implement-plan-issue-4891-deny-gh-api-jq-cli-flags): clean — conclusion success, tracker #3576 findings=0 followups_created=0

## Validation
- Cycle 1 — run 36638738895 2026-09-29 (target_ref: claude/implement-plan-issue-4891-deny-gh-api-jq-cli-flags, authorized head fadfddf): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 289s)

## Completion
- Merged into the project branch: phase 1 PR #5033
- Project branch synced with `main`: fadfddf (before security and validation), 2a9a73e (this stage, clean merge of ccd2ebc; `tests/test_gh_api_write_guard.py` 198 passed)
- Completion PR (this log commit) — doc moved to docs/completed/issue-4891-deny-gh-api-jq-cli-flags-plan.md
- Final PR #4920 draft — marked ready by the final-merge stage

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
- Protected-path approval: phase 1 — A, twin-first per Q40 (2026-09-29). Operator comment 5882155756 on #4891, reconfirmed in session session_011gNtd9qbns8WSKTLRvHP2g.
- Resumed in session session_011gNtd9qbns8WSKTLRvHP2g (Auto mode). The Auto-mode classifier denied removing `ai:claude-blocked` and pushing the project-branch sync (`[Auto-Mode Bypass]`), even after the operator reconfirmed in the session; those steps are left to the operator.
- AD-5 check: `.claude/hooks/permission_prompt_logger.py` records `PermissionDenied` only for classifier denials, and a `PreToolUse` hook deny is not one, so no logger or filer change (AD-5 stays A).
- Phase 1 verification: `tests/test_gh_api_write_guard.py` in a twin overlay (root hook replaced by the twin) — 198 passed, 13 of them new. In the real tree 11 fail until the twin sync, as expected: `test_template_parity` and the 10 new deny cases, which load the root hook (187 passed).
