# Implement-Plan Log — Guard differential: a warning no longer excuses a loosened guard

- Plan: docs/completed/issue-5325-fail-warned-guard-loosening-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5325   Progress comment: 5902398794
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-5174-guard-differential-check
- Project branch: claude/implement-plan-issue-5325-fail-warned-guard-loosening   Final PR: #5363 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-5174-guard-differential-check) — the base is not the default branch, so steps 12–13 do not run (Issue Mode)
- Waiting on: completion PR (claude/implement-plan-issue-5325-fail-warned-guard-loosening-complete → the project branch)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_019WGh3J49ycyYL7KPVjubhj (reused)   safety net / hand-back: see the validation 1/3 read-result stage report
- Last updated: 2026-09-30
- Last note: validation cycle 1 (run 36670737898, target_ref = the project branch) passed 10/10; no validation-fix PR, so no conformance re-run. Plan moved to docs/completed/ in the completion PR; next is final-merge (mark final PR #5363 ready, then close #5325 after it merges into the #5174 branch).

## Phases
1. [x] Phase 1 — fail on a warned loosening (scripts/guard_differential.py rule, tests, docstring, remediation text, ci.yml comment, agents.md, #5174 changelog fragment)   — PR #5366 merged 2026-09-30; review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-30: CONFORMANT (Step 3 COMPLETE, Step 4 CONCERNS) — conformance-fix PR #5455 (merged 2026-09-30, commit 50fbdee) for 1 CONCERN: stale `tests/guard_corpus/*.txt` headers (pre-security)
- Run 2 — 2026-09-30: CONFORMANT (Step 3 COMPLETE, Step 4 CONCERNS: the AD-4 inventory gap, inherited from #5174) — no fix PR (pre-security)

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` returned skip: true)

## Validation
- Cycle 1 — run 36670737898 2026-09-30 (target_ref: claude/implement-plan-issue-5325-fail-warned-guard-loosening; authorized as a stacked target by main's validate.yml, #4746): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 288s); no fix PR

## Completion
- Completion PR (branch claude/implement-plan-issue-5325-fail-warned-guard-loosening-complete) open — doc moved to docs/completed/issue-5325-fail-warned-guard-loosening-plan.md
- Final PR #5363 draft (into claude/implement-plan-issue-5174-guard-differential-check)

## Activation
- n/a: the base branch is the #5174 project branch, so the change goes live with that project's final PR #5185 (Issue Mode)

## Auto-decisions
- AD-1 [plan, 2026-09-30] How far should a warning count once the head is less strict? — Picked: A — not at all: every drop in strictness is a regression, and the warning is only printed. Alternatives: B — keep the exemption when the head answers `ask` or `none`; C — keep the exemption only for `ask`. Why: §1; a `none` with a warning still hands a formerly blocked shape to the normal permission flow, and the issue asks for failure "regardless of `systemMessage`". Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which changelog entry records the fix? — Picked: A — correct the one phrase in the unreleased `changelog.d/5174-guard-differential-check.md` and add no new fragment. Alternatives: B — add `changelog.d/5325-fail-warned-guard-loosening.md` with `<!-- changelog: security -->` as well. Why: the warning exemption never reached `main` or `stable`, so a separate "fixed" entry would describe a behaviour no consumer saw (§20.F), while the #5174 fragment would otherwise describe the wrong rule. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] Should the #5174 plan and progress log be updated to the new rule? — Picked: A — no, they stay as that project's record; the rule is corrected in the code, tests, `agents.md`, `ci.yml`, and the changelog fragment. Alternatives: B — also edit the #5174 plan's Approach paragraph. Why: §5, and those files record what #5174 planned and did. Applied in: no code change. Status: pending review
- AD-4 [conformance 2/3, 2026-09-30] docs/INVENTORY.md does not document scripts/guard_differential.py, so tests/inventory_parity.py fails on the project branch and on its base (inherited from #5174 PR #5187; ci.yml runs only on PRs into main/stable, so no PR of this project ran it). Fix it in this project? — Picked: A — no; it is the #5174 project's defect and surfaces on its final PR #5185 into main, where CI runs inventory parity; record it in the log's Notes and list it in this project's final PR body. Alternatives: B — add the INVENTORY entry in a conformance-fix PR here. Why: §5, and three sibling stacked projects (#5326, #5327, #5328) target the same base, so duplicate edits would conflict. Applied in: no code change. Status: pending review

## Lessons
- [source:intervention] When a change reverses a rule, grep the whole module docstring, its summary line included, for wording tied to the old rule, not only the paragraph the plan names. (files: scripts/guard_differential.py)
- [source:conformance] When a rule changes, grep every file that describes it for the old wording, test fixtures and corpus comments included, not only the files the plan lists. (files: tests/guard_corpus/pr_merge_status_guard.txt, tests/guard_corpus/pr_watch_guard.txt, tests/guard_corpus/gh_api_write_guard.txt, tests/guard_corpus/inline_edit_guard.txt)
- [source:conformance] A new script under scripts/ needs its docs/INVENTORY.md entry in the same PR; tests/inventory_parity.py runs only in ci.yml, which stacked project PRs never trigger, so run it locally. (files: docs/INVENTORY.md, scripts/guard_differential.py)

## Notes
- Base branch is the open #5174 project branch (final PR #5185, draft); every stage checks whether it merged first (Issue Mode "A base branch that merges moves the project").
- No phase touches `.claude/**`, so no protected-path approval is needed.
- AD-4: `tests/inventory_parity.py` fails on this project branch and on its base because docs/INVENTORY.md has no entry for scripts/guard_differential.py (added by #5174 PR #5187). It is left to the #5174 project and is listed in final PR #5363's body.
