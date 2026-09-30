# Implement-Plan Log — Treat `.claude/scripts/**` as a guard path in the Claude twin sync

- Plan: docs/plans/issue-5609-guard-synced-claude-scripts-plan.md
- Source issue: shubhodeep1/coding-workflows#5609
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5609-guard-synced-claude-scripts   Final PR: #5650 draft (base claude/implement-plan-issue-4785-twin-first-claude-sync)
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR 1 (branch claude/implement-plan-issue-5609-guard-synced-claude-scripts-conformance-fix-1)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_019Lc5auXqLNUF8Vdips7jGB   (safety net and hand-back ids for the conformance-fix wait are in the conformance 1/3 stage report and the next resume block)
- Last updated: 2026-09-30
- Last note: conformance run 1 (session session_01CemB7XtbmJfCvUD2ohXit1): CONFORMANT, Implemented COMPLETE, Correctness CONCERNS (two stale sibling texts still grouped scripts with the workflow-merged commands); fix PR 1 opened against the project branch.

## Phases
1. [x] Phase 1 — guard `.claude/scripts/**` in the twin sync (code, tests, docs; no `.claude/**` path is edited; protected paths: none)   — PR #5655 merged 2026-09-30; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Implemented COMPLETE: G1–G4 map to `scripts/claude_twin_sync.py:95`, tests, label contract, docs; Correctness CONCERNS: `.github/workflows/review_autofix.yml:549` gate comment and `docs/operations/master-session.md:130-131` duty table still grouped scripts with the workflow-merged commands) — fix PR 1 on `claude/implement-plan-issue-5609-guard-synced-claude-scripts-conformance-fix-1` (pre-security)

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How should synced scripts stop running with session privileges unreviewed? — Picked: A — add `scripts/` to `GUARD_PATH_PREFIXES` (owner-reviewed sync PR, #5246/#5247 CI rules). Alternatives: B — run synced scripts without session credentials behind least-privilege helpers; C — list the scripts in `UPSTREAM_ONLY_PATHS`. Why: extends the existing guard (§5); B is an architectural rewrite; C restores the protected-path stop. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-30] Should `.claude/commands/**` become a guard path too? — Picked: A — no. Alternatives: B — guard commands too. Why: commands are instructions at CLAUDE.md's trust level, still bound by permission rules and the classifier; scripts and hooks execute without either (§5). Applied in: no code change. Status: pending review
- AD-3 [plan, 2026-09-30] Should the posted status description `No hook or settings change` change? — Picked: A — keep it. Alternatives: B — rename it to include scripts. Why: still true, and a change re-posts a status on every open non-guard head (§5, §15). Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-30] How are the parent project's docs kept accurate? — Picked: A — correct the #4785 plan's guard lines, own changelog fragment only. Alternatives: B — also edit the #4785 fragment; C — change no parent doc. Why: the plan text is what the parent's activation check reads; a fragment belongs to its PR (§20). Applied in: phase 1. Status: pending review

## Lessons
- [source:conformance] When a change moves paths from one class to another (here scripts from auto-merged to owner-merged guard paths), grep every prose description of both classes repo-wide, including comments in other workflows and operations tables, not only the docs the plan lists. (files: .github/workflows/review_autofix.yml, docs/operations/master-session.md)

## Notes
- Security pass: skip (`security_pass_skip.py` → `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`).
- Session started by the Claude issue dispatcher (routine `implement-issue #5609`); session session_01RJHqprScG1RPCYWzJ6aqc9, permission mode auto.
- Issue progress comment id 5909404971.
- Phase 1 verification (2026-09-30): the 10 new or changed assertions in tests/test_claude_twin_sync.py fail on the pre-fix script and pass with it; 26 related suites 1124 passed, 6 skipped; CI ruff (E,F) clean; inventory parity ok; `claude_twin_sync.py check --base origin/main` ok. `workflow-templates/CLAUDE.md` is a symlink to CLAUDE.md, so one edit covers both.
- Base branch check: PR #4804 (head = the base branch) is open, not merged, so the base has not moved.
- Conformance run 1 checks (2026-09-30): 13 suites 617 passed, 3 skipped (twin sync, labels, implement-plan / implement-issue commands, session titles, CLAUDE.md sections, changelog contract, workflow size, fixer mode, PR sweep, label helpers, permission prompts); ruff (E,F) clean; inventory parity ok; `claude_twin_sync.py check` against the merge-base with `origin/main` (d1e530e) ok.
- `claude_twin_sync.py check --base origin/main` against `main`'s tip (238f457) reports `.claude/hooks/gh_api_write_guard.py`: `main` gained the #4619 hook sync (70b5493) after the #4785 base last merged `main`. It is not this project's change and clears when the base merges `main` again; a PR's CI checks the merge ref, which carries that sync.
- Base branch check (conformance 1/3): no merged PR has head claude/implement-plan-issue-4785-twin-first-claude-sync, so the base has not moved.
