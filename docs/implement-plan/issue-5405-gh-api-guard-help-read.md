# Implement-Plan Log — gh api guard: treat a bare `gh api --help` / `gh api -h` as a read

- Plan: docs/plans/issue-5405-gh-api-guard-help-read-plan.md
- Source issue: shubhodeep1/coding-workflows#5405
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5405-gh-api-guard-help-read   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from main; phase 1 starting (twin-first).

## Phases
1. [ ] Phase 1 — bare `gh api --help` / `gh api -h` is a read   — protected paths: .claude/hooks/gh_api_write_guard.py (twin-first via workflow-templates/.claude/hooks/gh_api_write_guard.py)
   - [ ] twin hook: `_HELP_ONLY_GH_API_ARGS` + exact-match read branch in `evaluate`; docstring `read` entry
   - [ ] tests: bare `--help` / `-h` read (allow); combos ask; `| grep` no decision; safe helpers allow; hidden calls ask (run against the twin)
   - [ ] CLAUDE.md + workflow-templates/CLAUDE.md §23.H read row
   - [ ] agents.md and README.md guard notes
   - [ ] changelog.d/5405-gh-api-guard-help-read.md (`changed`)
   - Done: new tests pass against the twin; the rest of tests/test_gh_api_write_guard.py passes except test_template_parity (red until the twin sync)

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] Where should the bare-help check live? — Picked: A — a small exact-match branch in `evaluate` before `parse_gh_api_args`. Alternatives: B — special-case `--help` inside `parse_gh_api_args`; C — add `--help`/`-h` to `_BOOL_FLAGS`. Why: A leaves the parser, and so every other `--help` combination's `Unreadable`, unchanged (§5); C would make `gh api --help repos/o/r` a read, which the issue forbids. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] How do the new tests exercise the behaviour before the twin sync? — Picked: A — load the twin as a second module and run the new cases against it. Alternatives: B — run them against `.claude/hooks/` (red until the sync); C — parametrize over both copies. Why: the interim twin-first rule says tests of new `.claude/` behaviour read the twin so they pass before the sync. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Where is the loosening declared while #5174's `scripts/guard_differential.py` is not on the base branch? — Picked: A — an `Intended loosening:` section in the phase PR body and the final PR body, plus the plan's Notes. Alternatives: B — hold the project until #5174 merges; C — add a corpus file ahead of #5174. Why: A records the reason where #5174 will look without creating files #5174 owns (§5). Applied in: phase 1 PR and final PR bodies. Status: pending review
- AD-4 [plan, 2026-09-30] Which doc copies change? — Picked: A — `CLAUDE.md` and its byte-identical twin `workflow-templates/CLAUDE.md`, plus `agents.md` and `README.md`. Alternatives: B — root `CLAUDE.md` only. Why: the two copies are identical today and consumer repos receive the twin. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-30)
- Security pass: run (`security_pass_skip.py`: no skip label).
