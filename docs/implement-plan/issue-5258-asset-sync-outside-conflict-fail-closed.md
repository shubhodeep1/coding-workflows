# Implement-Plan Log — Never continue a Claude fix on stale guards after a Claude-asset sync conflict

- Plan: docs/completed/issue-5258-asset-sync-outside-conflict-fail-closed-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5258 (https://github.com/shubhodeep1/coding-workflows/issues/5258)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4952-sync-claude-assets-at-session-start
- Project branch: claude/implement-plan-issue-5258-asset-sync-outside-conflict-fail-closed   Final PR: #5281 draft (into claude/implement-plan-issue-4952-sync-claude-assets-at-session-start)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4952-sync-claude-assets-at-session-start)
- Waiting on: the completion PR from claude/implement-plan-issue-5258-asset-sync-outside-conflict-fail-closed-complete
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01Lqubj5jYNAWVED9ASk8Vr6   safety net and hand-back: see the completion stage report
- Last updated: 2026-09-30
- Last note: completion stage: plan moved to docs/completed/; conformance run 1 CONFORMANT; security skipped (plan header); validation skipped under Q1: A (covered by #4952's project validation). Next: final-merge 1/1 marks #5281 ready and closes #5258 with `ai:merged` once it merges.

## Phases
1. [x] Phase 1 — resolve or fail closed on an outside-`.claude/` Claude-asset sync conflict — protected paths: .claude/commands/fix-claude-pr.md, .claude/commands/implement-plan-claude.md — PR #5291 merged 2026-09-29 (71e80e2, after the `[claude-twin-sync]` copy 8d422d9); review rounds: 0; interventions: 0

## Conformance
- Run 1 — 2026-09-29: CONFORMANT — no fixes (pre-security). Every plan criterion traces to the merged code: neither command says the fix continues on the unsynced head (`.claude/commands/fix-claude-pr.md:46`, `.claude/commands/implement-plan-claude.md:185`, Claude-asset sync step 5); both carry the resolve-inside-the-sync-merge rule and its `git merge --abort` and stop path; the `.claude/` copies are byte-identical to their twins; `agents.md` and `changelog.d/5258-asset-sync-outside-conflict-fail-closed.md` (`security`) are updated; the scratch-repository test shows the default branch's hook in the working tree while the merge is stopped on an outside conflict. Checks on 33acff4: asset-sync, session-start drift, implement-plan, implement-issue, check-in and CI-split suites 371 passed, 1 skipped; every suite that reads `fix-claude-pr.md` plus the changelog-fragment and assembler tests 427 passed; the full `tests/` run did not finish in 22 minutes and `tests/test_workflow_retro.py` needs Python 3.12 (both unrelated). Note, not a defect: `git commit --no-edit` on a conflicted merge keeps Git's `# Conflicts:` lines in the commit body; the documented `[claude-asset-sync]` subject is unchanged.

## Security pass
- Skipped (ai:security: automation-produced issue) — verified by .claude/scripts/security_pass_skip.py on 2026-09-29

## Validation
- Skipped (covered by #4952's project validation) — Q1: A, 2026-09-30 (issue comment 5901696900, standing decision Q17: A in docs/operations/master-session.md). `validate.yml` step "Authorize explicit validation target" accepts `target_ref` only when exactly one open PR with that head targets `main`, and final PR #5281 goes into the #4952 project branch, so the run would fail before validating anything. #4952's final PR #4995 goes into `main` and runs its own security audit and runtime validation on a branch that contains this fix. Long-term fix: #4734.

## Completion
- Completion PR from claude/implement-plan-issue-5258-asset-sync-outside-conflict-fail-closed-complete (open) — doc moved to docs/completed/issue-5258-asset-sync-outside-conflict-fail-closed-plan.md
- Final PR #5281 draft

## Activation
- n/a: the base is the #4952 project branch, so this change goes live with project #4952.

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should a Claude fix handle a Claude-asset sync merge that conflicts only outside `.claude/`? — Picked: A — resolve the conflict inside the sync merge under the caller's §12 conflict rules, and when the resolution is not evident, `git merge --abort` and stop like the caller's unresolvable conflict (fail closed); never continue on the unsynced head. Alternatives: B — always fail closed: abort, hold, and wait for a human; C — overlay the default branch's `.claude/hooks` and `settings.json` on the working tree and continue on the unsynced head. Why: current guards run through the whole fix without parking every routine conflict PR on a human or slipping unreviewed guard files into the fix. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Should the rule cover only `/fix-claude-pr` or every PR-head caller of the Claude-asset sync? — Picked: A — every PR-head caller, as one shared rule in Claude-asset sync step 5. Alternatives: B — `/fix-claude-pr` only. Why: `/implement-plan-claude` steps 7 and 7a have the same gap in the same flow. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which commit subject does a sync merge that resolved a conflict carry? — Picked: A — the sync's documented `[claude-asset-sync] merge <source> for .claude/ guard updates` (kept by `git commit --no-edit`). Alternatives: B — `[claude-merge-resolve] merge <source>`. Why: one documented subject; both break the consecutive `[claude-autofix]` count the same way. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the phase also stop fixes on branches the sync skips (step 3: a base other than the default branch)? — Picked: A — no; non-goal. Alternatives: B — fail closed on every skipped sync too. Why: the finding names the aborted-merge path only, and B would stop every fix on a `stable`- or PR-based branch. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] What happens to the report phrase `claude_assets=stale (conflict outside .claude/)`? — Picked: A — drop it. Alternatives: B — keep it documented as an alias. Why: the state no longer occurs, it never reached the default branch, and nothing parses it. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:security] A "conflict only outside `.claude/`" rule for a guard-sync merge must never abort and continue on the unsynced head: the sync's source is the PR's base, so resolve the conflict inside that merge (the merged `.claude/` files are already in the working tree) or abort and stop. (files: .claude/commands/fix-claude-pr.md, .claude/commands/implement-plan-claude.md)

## Notes
- Issue mode: base branch `claude/implement-plan-issue-4952-sync-claude-assets-at-session-start` (open draft final PR #4995) is not the default branch, so the final-merge stage closes #5258 explicitly with `ai:merged`, and activation is `n/a (base …)` unless the base moves onto `main`.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
- 2026-09-29: phase 1 twin-sync blocker (comment 5900525014) answered A (comment 5900788401): the master session copied both twins into `.claude/commands/` as `[claude-twin-sync]` commit 8d422d9 on the phase branch; PR #5291 then merged as 71e80e2. Project branch synced with the base as 33acff4 (clean merge of 4200c38).
- 2026-09-29/30: validation 1/3 blocked as Q1 (comment 5901364299, stacked project); Q1: A (comment 5901696900). Conformance run 1 was not re-run.
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5258#issuecomment-5900167161
