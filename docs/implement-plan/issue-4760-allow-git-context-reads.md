# Implement-Plan Log — Allowlist the read-only git context reads that preface a default-branch lookup

- Plan: docs/plans/issue-4760-allow-git-context-reads-plan.md
- Source issue: shubhodeep1/coding-workflows#4760
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4760-allow-git-context-reads   Final PR: (opened after this commit; see the issue progress comment)
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none — human answer on #4760 (protected-path approval for phase 1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (a BLOCKED stop arms no wait)
- Last updated: 2026-09-28
- Last note: Phase 1 edits `.claude/settings.json` (a protected path) and the log has no `Protected-path approval: phase 1` line, so the phase did not start. Asked on #4760 (CLAUDE.md §28.C).

## Phases
1. [ ] Phase 1 — allow the two read-only git context reads (`Bash(git remote -v)`, `Bash(git branch --show-current)`), test, docs, changelog fragment — protected paths: `.claude/settings.json` (template copy `workflow-templates/.claude/settings.json`)
   - [ ] both settings files carry the two exact rules and no `git remote` / `git branch` wildcard; files byte-identical
   - [ ] new test in `tests/test_gh_api_write_guard.py` passes, and the rest of that file passes
   - [ ] `agents.md` Permissions note updated
   - [ ] `changelog.d/4760-allow-git-context-reads.md` added

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should the #4760 pattern stop depending on the Auto-mode classifier? — Picked: A — add exact read-only allow rules `Bash(git remote -v)` and `Bash(git branch --show-current)` to both settings files. Alternatives: B — make `gh_api_write_guard.py` treat the two git reads as safe helpers; C — only change command-file prose to run the default-branch read on its own. Why: the docs say narrow allow rules resolve before the classifier, while a hook `allow` does not skip it and only covers commands with `gh api`; prose alone does not stop recurrence. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Exact or wildcard git rules? — Picked: A — exact rules only. Alternatives: B — `Bash(git remote *)` and `Bash(git branch *)`. Why: wildcards would approve `git remote set-url|add|remove` and `git branch -D|-m`, which §23.I forbids widening. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Fold the overlapping #4759 pattern (`… && wc -l <file>`) into this project? — Picked: A — no, keep to #4760. Alternatives: B — also allowlist `wc -l`. Why: `/implement-issue-claude` runs one issue per chain, and #4759 has its own queued project. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-28] #4750 and #4751 show allowlisted calls denied in the same outage window, so the fix may not prevent a denial during a full classifier outage. Proceed? — Picked: A — proceed, and record the residual risk. Alternatives: B — stop and ask whether to close #4760 as a transient outage. Why: the documented precedence says narrow allow rules skip the classifier; closing an issue this session did not open is a §23.C ask-first operation. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-28] Where does the new test live? — Picked: A — `tests/test_gh_api_write_guard.py`, beside the existing settings parity and wiring tests. Alternatives: B — `tests/test_permission_prompts.py`. Why: that file already loads both settings paths and the guard, and it has its own `ci.yml` step. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- 2026-09-28: started by `/implement-issue-claude` in session `session_01WnzUW5DYDvoyyw8jz4JJ5t` (Auto mode, claude-opus-5-5), dispatched by routine `trig_01DacFZSsp2FMqKXivyN8Shv`.
- 2026-09-28: security pass `run` (`security_pass_skip.py`: no skip label).
- 2026-09-28: phase 1 is marked `protected paths: .claude/settings.json`. It is blocked before starting until a `Protected-path approval: phase 1 — <letter> (<date>)` line is recorded here (CLAUDE.md §28.C).
