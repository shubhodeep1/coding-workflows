# Implement-Plan Log — Route file edits through the Edit and Write tools, not inline interpreter heredocs

- Plan: docs/plans/issue-4678-edit-files-without-python-heredocs-plan.md
- Source issue: shubhodeep1/coding-workflows#4678
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4678-edit-files-without-python-heredocs   Final PR: #4684 draft
- Status: IN_PROGRESS
- Stage: conformance 1/3
- Activation: not started
- Waiting on: conformance fix PR from `claude/implement-plan-issue-4678-edit-files-without-python-heredocs-conformance-fix-1` (the PR that carries this line)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_017anXdnT2hzkCybLyi5nLJY   (safety net and hand-back ids are in the stage report and the next `— resume.` block)
- Last updated: 2026-09-28
- Last note: conformance run 1 CONFORMANT (correctness: CONCERNS); fix PR opened for two test/changelog findings; AD-7 left the permission_prompts.py issue-body sibling as is.

## Phases
1. [x] Phase 1 — file-edit rule and protected-path precision in CLAUDE.md §23.I (CLAUDE.md, tests/test_permission_prompts.py, agents.md, changelog.d/4678-edit-files-without-python-heredocs.md)   — PR #4696 merged 2026-09-28; review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-28: CONFORMANT (Implemented: COMPLETE; Correctness: CONCERNS) — conformance fix PR from `…-conformance-fix-1` (pre-security). Fixed: `tests/test_permission_prompts.py:399` did not pin the §23.I triage clause, since the no-retry paragraph also satisfies it (mutation check); the changelog fragment's lead ran to 9 sentences against §20.D's 3-5. Auto-decided: AD-7.

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Is this prompt by design, so the issue closes as not planned? — Picked: A — no, fix it: the heredoc edited `workflow-templates/.claude/commands/*.md` (commit e353799), which Claude Code does not protect (only the root `.claude/` and `~/.claude/` are protected), and the prompt came from the unparseable inline interpreter. Alternatives: B — close as not planned under §23.I's protected-path clause. Why: the path is not protected, so the prompt is avoidable. Closing an issue this session did not open is also a §23.C ask-first operation. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Where does the fix live? — Picked: A — CLAUDE.md §23.I, which every interactive session reads and consumer repos receive through the CLAUDE.md sync. Alternatives: B — `.claude/commands/implement-plan-claude.md` step 4 and its `workflow-templates/` twin; C — both. Why: no command instructs the heredoc, so this is the model's tool choice. A needs no protected-path edit, so the unattended chain can run without a `BLOCKED` stop. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Clarify §23.I's "protected-path edit (`.claude/**`)" clause? — Picked: A — yes, name the repository root's `.claude/**` and say `workflow-templates/.claude/**` is not protected. Alternatives: B — leave the clause as is. Why: the loose reading would have closed this fixable issue as not planned. The clause is the triage rule for every future `ai:permission-prompt` issue. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Fold sibling issue #4677 (`python3 * << *`, same session) into this project? — Picked: A — no, it keeps its own issue-mode project. Alternatives: B — fold it in. Why: `/implement-issue-claude` runs one issue per chain, and #4677's heredoc body is unknown, so it may not be a file edit. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-28] Does the change need a `changelog.d/` fragment? — Picked: A — yes, `changed`. Alternatives: B — none (docs only). Why: §20.A requires one for anything that changes what a consumer repo receives on the next `@stable` sync, and CLAUDE.md is synced. Applied in: phase 1 PR. Status: pending review
- AD-6 [phase 1/1, 2026-09-28] Add a rule that a blocked or denied write to the root `.claude/**` is never retried through another tool? — Picked: A — yes, in CLAUDE.md §23.I in this phase, with a contract test: the session stops at `Status: BLOCKED` naming each file and its exact edit, and the operator's watched session applies it. Alternatives: B — leave it out of scope. Why: operator decision Q15: A (2026-09-28), relayed by the supervising session `session_01VwSvLnEGmUoaQD42DKapiU` and confirmed by the owner's closing comment on #4677. It is the flow `claude-fixer-unattended-convergence` phase 2 used (commit 6b8a1a3), and it lives in CLAUDE.md, so the phase still needs no protected-path stop. Applied in: phase 1 PR. Status: pending review
- AD-7 [conformance 1/3, 2026-09-28] The issue body that `.claude/scripts/permission_prompts.py` files for every `ai:permission-prompt` issue (lines 351-352, and its `workflow-templates/.claude/scripts/` twin) still says "If the call edits `.claude/**` (a protected path no setting can approve) … close this issue as not planned", the loose reading AD-3 fixed in CLAUDE.md §23.I. Fix that sibling in this project? — Picked: A — no, leave the text: the issue body sends triagers to CLAUDE.md §23.I, which now defines the protected path precisely. Alternatives: B — change both copies in a conformance fix PR (a root `.claude/**` edit, so the stage stops at `Status: BLOCKED` for a watched session, §28.C); C — open a separate issue for it. Why: the plan's Non-goals exclude every `.claude/**` edit (AD-2), and A is the smallest change (§5) that needs no protected-path stop. Applied in: no code change. Status: pending review

## Lessons
- [source:conformance] When a plan narrows a rule in CLAUDE.md, grep the old wording in the text that scripts generate (issue bodies, PR comments built by `.claude/scripts/*`) and plan the sibling change or record why it stays. (files: CLAUDE.md, .claude/scripts/permission_prompts.py)
- [source:conformance] A contract test that pins a phrase must assert a string unique to the clause it guards; when another paragraph shares the phrase, assert the clause's full wording and check it with a mutation of that clause alone. (files: tests/test_permission_prompts.py)

## Notes
- Invoking session: session_01Fq1huPgbwLwakWQAhyse3J (started by the Claude issue dispatcher routine trig_01P61QC1Yzt48puVZajbAWG9, permission mode auto).
- Security pass: `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so the pass runs.
- Issue progress comment id: 5861812192.
- Phase 1 verification (2026-09-28): `tests/test_workflow_retro.py` fails collection on Python 3.11 (f-string backslash in `scripts/workflow_retro.py:794`) on main too; unrelated to this project, excluded from the local full-suite run.
- 2026-09-28 02:44Z: operator scope addition (Q15: A) arrived through the routine `issue #4678: operator scope addition` (trig_01PKqDAGmhjuBQCsPVBYSYDx). Checked against GitHub before acting: #4677 was closed as a duplicate by the owner at 02:37Z, with a comment naming this rule, and commit 6b8a1a3 is on the phase-2 branch of `claude-fixer-unattended-convergence`. Recorded as AD-6.
- Phase 1 scoped test run: 46 doc-reading test files. The only failure is `test_implement_post_codex_recovery.py::test_review_pipeline_integration_chain_module_runs_clean` (`gawk: command not found` in `scripts/review_issue_ledger.sh`), an environment gap in this container that is unrelated to the change.
- Stale Routine sweep (2026-09-28): deleted 1 ended Routine (`implement-plan issue-4620-intake-authorize-target-issue: checker instructions`); none for this slug.
- Conformance 1/3 (session_018KbYmheTjsgX2jWs7YeYNQ, 2026-09-28): synced the project branch with `main` (clean merge, cbf1705). Full suite on the project branch: 4640 passed, 45 failed, 2 skipped (`tests/test_workflow_retro.py` excluded, since it fails collection on Python 3.11). The same 45 fail on a clean export of `origin/main` (31 are `gawk: command not found`, the rest `scripts/workflow_retro.py:794` f-string syntax on Python 3.11), so none comes from this project.
- Conformance 1/3 observation, not a finding: CLAUDE.md §28.C ("A phase that must edit `.claude/**`") and `/implement-plan-claude` step 3 still use the unqualified path. Read loosely, they stop a phase that edits only `workflow-templates/.claude/**`. That fails safe (an extra stop, never a skipped one) and is outside the §23.I triage behaviour this plan changes.
