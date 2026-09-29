# Implement-Plan Log — Parse issue branch lines in linear time in issue_body_integration_branch

- Plan: docs/plans/issue-4956-linear-issue-branch-parser-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#4956   Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
- Project branch: claude/implement-plan-issue-4956-linear-issue-branch-parser   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from the #4813 project branch; phase 1 starting.

## Phases
1. [ ] Phase 1 — linear-time parser for `issue_body_integration_branch` (`scripts/gh_helpers.sh`, its test, changelog fragment)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verdict recorded in the plan's Notes).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Which copies of the branch-line grammar does this issue fix? — Picked: A — only `issue_body_integration_branch` in `scripts/gh_helpers.sh` (the flagged location, both of its patterns). Alternatives: B — also `scripts/orchestrate_lib.py` and `scripts/resolve_integration_ref.sh`. Why: §5; the other copies are on `main`, outside this project's diff, and route orchestrator planning, so changing them belongs in its own issue. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] What counts as whitespace inside a line? — Picked: A — every character `\s` matched before except `"\n"` (tabs, `\r`, Unicode spaces), so single-line results stay identical to the Python parser. Alternatives: B — space and tab only, as the issue words it, which would stop matching values followed by `\r` or other whitespace that parse today. Why: §1 without breaking backward compatibility; the issue's goal (no newline spanning) is met either way. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] How to make the parse linear? — Picked: A — a line-by-line string parser with no regex backtracking. Alternatives: B — the same regexes applied per line (still cubic within a line: 29.7 s for 2,000 spaces); C — regexes plus a line-length cap (changes results for long lines, still quadratic up to the cap); D — possessive quantifiers (Python 3.11+ only). Why: the only option that is linear for every input with no change in results for single-line forms. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode (CLAUDE.md §28.A): started by the Claude issue dispatcher routine in session session_019VGvihqo4vEMB83gP6DWSc (Auto mode).
- This session had no `mcp__github__*` tools; GitHub reads and routine writes went through `gh api` REST (the repo's SessionStart hook installed `gh`, since the repo was attached mid-session).
