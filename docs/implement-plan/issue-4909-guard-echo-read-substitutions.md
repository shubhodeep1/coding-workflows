# Implement-Plan Log — Approve read-only `$(gh api …)` substitutions in `echo`, and name the approved shape when the `gh api` guard asks

- Plan: docs/plans/issue-4909-guard-echo-read-substitutions-plan.md
- Source issue: shubhodeep1/coding-workflows#4909 (https://github.com/shubhodeep1/coding-workflows/issues/4909)
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4909-guard-echo-read-substitutions   Final PR: opened as a draft right after this commit, into `claude/implement-plan-issue-4786-guard-allow-read-loops` (number in the issue's progress comment)
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none (#4786's phase 1 must merge into its project branch first; question on #4909)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (blocked before the first wait; no checker armed)
- Last updated: 2026-09-29
- Last note: phase 1 depends on #4786's loop frame (`_is_approvable_read_loop`), which is not on the base yet: #4786 has no phase PR (its project branch head `132046a` holds only its log and base merges). The project stopped before phase 1 and asked on #4909.

## Phases
1. [ ] Phase 1 — read-only `echo` substitutions and approved-shape hints in the `gh api` guard — protected paths: `.claude/hooks/gh_api_write_guard.py` (twin-first per Q40)
   - [ ] precondition: `_is_approvable_read_loop` (#4786 phase 1) is on the base after the step 2 sync
   - [ ] `_READ_SUBSTITUTION_PLACEHOLDER`, `_read_substitution_call`, `_rewrite_echo_read_substitutions`, `_APPROVED_SHAPES_HINT` and the `evaluate` rewrite step in `workflow-templates/.claude/hooks/gh_api_write_guard.py` (plus docstring)
   - [ ] `tests/test_gh_api_write_guard.py`: `ECHO_READ_SUBSTITUTION_ALLOWED`, `ECHO_READ_SUBSTITUTION_ASK`, hint and hook-process cases
   - [ ] CLAUDE.md §23.H (`write` row, decision paragraph) and §23.D.4, mirrored byte-for-byte to `workflow-templates/CLAUDE.md`
   - [ ] `agents.md` guard paragraph
   - [ ] `changelog.d/4909-guard-echo-read-substitutions.md`
   - [ ] Q40 delivery: phase PR with only the twin changed under `.claude`-mirrored paths, `hold` claim, `ai:claude-blocked` twin-sync request; `[claude-twin-sync]` by the supervising session
   - Done when the plan's pytest run passes (against the twin, then after the sync), both `CLAUDE.md` copies `cmp` identical, the incident command → `allow` through the hook process, and a `-X POST` substitution → `ask`

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which branch is this project built on? — Picked: A — #4786's project branch `claude/implement-plan-issue-4786-guard-allow-read-loops`, with the phase gated on #4786's phase 1 having merged there. Alternatives: B — `main`, waiting for the whole #4786 project to merge; C — `main` now, with a copy of the loop frame. Why: the issue allows "on top of its branch" and forbids duplicating the parser. Applied in: no code change. Status: pending review
- AD-2 [plan, 2026-09-29] Where does the substitution logic live? — Picked: A — a rewrite step in the existing guard's `evaluate`, reusing `_is_approvable_command` and #4786's `_is_approvable_read_loop`. Alternatives: B — a separate `PreToolUse` hook. Why: one file, no duplicated parser (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] A read substitution outside an `echo` argument? — Picked: A — today's result (ask, hidden call). Alternatives: B — no decision. Why: the issue allows it only in `echo` arguments; §1. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Unquoted `$(gh api …)` too? — Picked: A — no, double-quoted only; unquoted keeps today's no-decision. Alternatives: B — both. Why: unquoted output word-splits and globs. Applied in: phase 1 PR. Status: pending review
- AD-5 [plan, 2026-09-29] Which calls count as a read inside the substitution? — Picked: A — REST `GET` / `HEAD`, no `--input`, no `-F …=@file`, safe headers only; GraphQL keeps asking. Alternatives: B — also non-mutation GraphQL. Why: the issue names GET/HEAD. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-29] Rewritten command not approvable? — Picked: A — evaluate the original command exactly as today (ask). Alternatives: B — no decision. Why: §1; the change only turns today's ask into allow for the approved shapes. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-29] Where may `$` appear inside the substitution? — Picked: A — nowhere at top level; in an approved loop only the loop variable, in the endpoint before `?`. Alternatives: B — any `$VAR`. Why: §1. Applied in: phase 1 PR. Status: pending review
- AD-8 [plan, 2026-09-29] How is the approved equivalent named? — Picked: A — a fixed hint string appended to the hidden-call ask and to a write ask whose command holds a `for` loop. Alternatives: B — a hint rebuilt from the command. Why: never echoes attacker-shaped text; simple to test. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Started by the Claude issue dispatcher (trigger `trig_01QG6mcMtan3c5o5mDN9eqjj`) in session `session_01TmWfyA7xEyRXABE3beLmAx`, Auto mode. The session had no `gh` and no GitHub MCP tools at start; `gh` was installed by running `.claude/hooks/session-start.sh` after the repo was attached, and GitHub writes went through `gh api` REST.
- Security pass: run (`security_pass_skip.py` printed `skip: false`, reason `no skip label`).
- Protected-path approval: phase 1 — twin-first per Q40 (issue #4909 Constraints, owner-authored, 2026-09-29).
- Step 4 dependency stop: the plan's phase 1 precondition (#4786's loop frame on the base) does not hold. The block is recorded in this first commit because the step 3a log commit is the only direct push before phase 1, and nothing is written after the stop. The question is on #4909 (`<!-- ai:claude-blocked:v1 -->`, label `ai:claude-blocked`).
