# Implement-Plan Log — Route file edits through the Edit and Write tools, not inline interpreter heredocs

- Plan: docs/plans/issue-4678-edit-files-without-python-heredocs-plan.md
- Source issue: shubhodeep1/coding-workflows#4678
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4678-edit-files-without-python-heredocs   Final PR: (opened after this commit)
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-28
- Last note: project branch opened with the plan and this log; phase 1 starts next in the same session.

## Phases
1. [ ] Phase 1 — file-edit rule and protected-path precision in CLAUDE.md §23.I (CLAUDE.md, tests/test_permission_prompts.py, agents.md, changelog.d/4678-edit-files-without-python-heredocs.md)

## Conformance

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

## Lessons

## Notes
- Invoking session: session_01Fq1huPgbwLwakWQAhyse3J (started by the Claude issue dispatcher routine trig_01P61QC1Yzt48puVZajbAWG9, permission mode auto).
- Security pass: `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so the pass runs.
- Issue progress comment id: 5861812192.
- Stale Routine sweep (2026-09-28): deleted 1 ended Routine (`implement-plan issue-4620-intake-authorize-target-issue: checker instructions`); none for this slug.
