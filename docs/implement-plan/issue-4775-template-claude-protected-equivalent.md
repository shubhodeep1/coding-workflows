# Implement-Plan Log — Treat workflow-templates/.claude as protected-equivalent for unattended authorization

- Plan: docs/completed/issue-4775-template-claude-protected-equivalent-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#4775
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4678-edit-files-without-python-heredocs
- Project branch: claude/implement-plan-issue-4775-template-claude-protected-equivalent   Final PR: #4783 draft
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4678-edit-files-without-python-heredocs): the project ends after the final merge, and the final-merge stage closes #4775 and labels it `ai:merged`
- Waiting on: completion PR (the number is in the completion 1/1 stage report)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01QiSAKZHye73dtZzDqHuRaw (reused)   safety net and hand-back in the completion 1/1 stage report
- Last updated: 2026-09-29
- Last note: validation 1/3 skipped by operator (Q1: B on #4775); plan moved to docs/completed/ in the completion PR; next: final-merge 1/1.

## Phases
1. [x] Phase 1 — protected-equivalent template tree: CLAUDE.md §23.I/§28.C and the template parity contract test (CLAUDE.md, tests/test_claude_template_parity.py [new], tests/test_permission_prompts.py, .github/workflows/ci.yml, agents.md, changelog.d/4775-template-claude-protected-equivalent.md [new]); protected paths: none   — PR #4819 merged 2026-09-28 (ad00041); review rounds: 1; interventions: 0

## Conformance
- Run 1 — 2026-09-28: CONFORMANT — no fixes (pre-validation; security skipped; audited at ad00041). Every plan criterion maps to merged code; 707 tests pass (the plan's suite plus all 11 `test_template_parity` files); `yamllint -s` and `actionlint` pass on `ci.yml`; all five plan mutations fail the new test on a scratch worktree.

## Security pass
- Skipped: `Security pass: skip (ai:security: automation-produced issue)` in the plan header.

## Validation
- Cycle 1 — 2026-09-28: not dispatched. `validate.yml` ("Authorize explicit validation target") accepts a `target_ref` only when its single open PR goes into `main`; final PR #4783 goes into the #4678 project branch, so the run would fail with `target PR binding is missing or ambiguous` (as #4665's run 36378523375 did). Fix in progress: #4734. Stopped at `Status: BLOCKED` and asked on #4775 (comment 5872404887).
- Skipped by operator — 2026-09-28: `Validation: skipped by operator (covered by #4678's project validation)`, answer Q1: B on #4775 (comment 5880778711, then `/reclarify`). This project changes only CLAUDE.md wording and a CI contract test, and #4678's own validation runs on a branch that will contain it.

## Completion
- Completion PR (claude/implement-plan-issue-4775-template-claude-protected-equivalent-complete) open 2026-09-29 — doc moved to docs/completed/issue-4775-template-claude-protected-equivalent-plan.md
- Final PR #4783 draft (into claude/implement-plan-issue-4678-edit-files-without-python-heredocs)

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] How should CLAUDE.md treat `workflow-templates/.claude/**`? — Picked: A — keep "not a Claude Code protected path" for prompt triage (a prompt issue about it is fixed) and declare it protected-equivalent for authorization in §23.I and §28.C, since the sync copies it into consumer `.claude/`. Alternatives: B — revert #4678's clause to the unqualified `.claude/**`; C — leave the text as is. Why: A closes the authorization gap the audit found without undoing #4678's triage fix; C leaves a high-severity finding open. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Which deterministic control backs the rule? — Picked: A — a new CI contract test over the whole template tree: every template file needs a root twin and is byte-identical to it unless it is in an explicit divergence list whose template SHA-256 is pinned; no symlinks; wired into the existing §23.I `ci.yml` step. Alternatives: B — the same list by name only, with no digest; C — an auto-merge gate in `review_autofix.yml` that requires an owner approval for PRs touching either `.claude/` tree; D — wording only. Why: A fails closed on every template-only command, hook, or settings change and needs no protected-path edit. B lets a divergent command change silently. C changes the documented auto-merge contract for every project and consumer (§12.D), beyond §5 for this finding. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Also change `/implement-plan-claude` step 3 (and its template twin) to name `workflow-templates/.claude/**`? — Picked: A — no. CLAUDE.md §28.C, which step 3 cites and every session reads, names the twins, and the parity test is the deterministic backstop. Alternatives: B — yes, as a root `.claude/**` edit that stops this unattended chain for a watched session (§28.C); C — file a separate issue. Why: A is the smallest change (§5) and needs no protected-path stop. The #4678 conformance run found that step 3's loose reading already stops such a phase. Applied in: no code change. Status: pending review
- AD-4 [plan, 2026-09-28] How is the change recorded in the changelog? — Picked: A — a new `security` fragment `changelog.d/4775-template-claude-protected-equivalent.md` that qualifies the #4678 entry; the #4678 fragment stays as is. Alternatives: B — also rewrite the #4678 fragment. Why: the #4678 statement stays true for Claude Code's own protection, one fragment per PR (§20.B), and B edits another project's entry. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:intervention] A tree-parity or allow-list walker must check symlinks before any name-based skip, and may skip `.gitignore`d runtime output (`__pycache__/`, `*.pyc`) only while git does not track it, because the `.claude/` sync copies every regular file (`find -type f`). (files: tests/test_claude_template_parity.py, .github/workflows/update_workflows.yml)
- [source:validation] An issue-mode project whose base is another project's branch cannot run `validate.yml` with `target_ref` until the explicit-target authorization accepts a final PR into a non-default base; check that binding before dispatching, and plan the validation route (wait for the fix, or an operator skip covered by the parent project's validation) up front. (files: .github/workflows/validate.yml)

## Notes
- Invoking session: session_013fp3SZXJQE3XBJt3SiWYkw (started by the Claude issue dispatcher routine trig_018FzWWEXvLsvqk8uNUGDReS, permission mode auto).
- Security pass: `security_pass_skip.py` printed `{"skip": true, "label": "ai:security", "reason": "ai:security: created and labelled by the issue automation"}`.
- Issue progress comment id: 5867385117.
- Base branch check (2026-09-28): the only PR whose head is the base branch is #4684 (open, draft, into `main`), so the base has not moved.
- Validation 1/3 stage (2026-09-28) stopped at `Status: BLOCKED` on the validation target binding; the project checker session_01QiSAKZHye73dtZzDqHuRaw was kept idle with no check-in pending.
- Completion 1/1 stage (2026-09-29): session_017oms6bdTdZpE3Tir7NJHWB, started by the dispatcher routine trig_01YLcgSLUGqzAaex4FD2T4PP after the `/reclarify`. Base branch check: #4684 is still open (draft, into `main`), so the base has not moved. The project branch already contained the base branch (no sync push). Re-ran the plan's tests plus `tests/test_lint_plan_archival_completeness.py` on the project branch: 105 passed. The zombie-checker cleanup (`list_sessions`) and the stale Routine sweep (`list_triggers`) were denied by the Auto-mode classifier and were skipped.
