# Implement-Plan Log — Allow the read-only `git remote -v`, `git branch --show-current`, and `wc -l` shapes without the Auto-mode classifier

- Plan: docs/plans/issue-4759-allow-read-only-git-wc-plan.md
- Source issue: shubhodeep1/coding-workflows#4759
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4759-allow-read-only-git-wc   Final PR: draft (number recorded with the first phase PR)
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none (protected-path approval for phase 1, asked on issue #4759)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: Phase 1 edits `.claude/settings.json` and `workflow-templates/.claude/settings.json` (protected paths, CLAUDE.md §28.C); stopped before the phase started and asked on #4759 how to run it.

## Phases
1. [ ] Phase 1 — allow the three read shapes (`git remote -v`, `git branch --show-current`, `wc -l *`) — protected paths: `.claude/settings.json`, `workflow-templates/.claude/settings.json`
   - Add the three exact rules to `.claude/settings.json` after `Bash(git rev-parse *)`.
   - Copy the file byte for byte to `workflow-templates/.claude/settings.json`.
   - Test in `tests/test_permission_prompts.py` over both settings paths.
   - `agents.md` bullet in the §23.I section; `changelog.d/4759-allow-read-only-git-wc.md` [new].
   - Done: both files carry the rules and stay byte-identical; the §23.I CI step and `tests/test_session_start_extract_repo_slug.py` pass.

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should the denied read chain be fixed? — Picked: A — add exact `permissions.allow` rules for `git remote -v`, `git branch --show-current`, and `wc -l *` in both settings copies. Alternatives: B — extend `gh_api_write_guard.py`'s safe helpers; C — reword `/implement-issue-claude` step 0 only; D — close the issue as a transient classifier outage. Why: the issue's fix list allows exact rules for reads, they approve the shape in any chain, and they are the smallest change (§5). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] How wide should the `wc` rule be? — Picked: A — `Bash(wc -l *)`. Alternatives: B — `Bash(wc *)`. Why: matches the observed shape and nothing more (§5). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Should the sibling permission-prompt issues (#4760 and the others from the same outage) be folded in? — Picked: A — no; note that #4760's pattern is covered and leave every sibling to its own project. Alternatives: B — fold #4760 in and close it. Why: one issue, one chain (`/implement-issue-claude` rules). Applied in: no code change. Status: pending review

## Lessons

## Notes
- Security pass: run (`security_pass_skip.py`: `no skip label`).
- Phase 1 is marked protected paths; no `Protected-path approval: phase 1` line yet. Asked on issue #4759 (CLAUDE.md §28.C). Record the answer here as `Protected-path approval: phase 1 — <letter> (<date>)` before the phase starts.
