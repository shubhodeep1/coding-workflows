# Implement-Plan Log — Merge claude/* PRs synchronously after the hold gate and cancel stale auto-merge

- Plan: docs/plans/issue-5565-claude-sync-merge-cancel-auto-merge-plan.md
- Source issue: shubhodeep1/coding-workflows#5565
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5565-claude-sync-merge-cancel-auto-merge   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from claude/implement-plan-issue-5316-gate-auto-merge-on-hold-claims

## Phases
1. [ ] Phase 1 — synchronous claude/* merge and stale auto-merge cancellation (scripts/review_enable_auto_merge.sh, review_autofix.yml gate + deterministic-skip-merge, gate docstring, tests, docs)

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified).

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-30] How does the review workflow merge a `claude/*` head the gate allowed? — Picked: A — a synchronous head-bound merge (`gh pr merge --squash --match-head-commit`, no `--auto`) right after the gate, in both paths. Alternatives: B — keep `--auto` and add hold-triggered cancellation; C — keep `--auto` and accept the window. Why: an enrollment cannot re-run the gate when checks settle (§1). Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] What happens when GitHub refuses the synchronous merge? — Picked: A — enroll nothing; log `AUTOFIX_AUTO_MERGE_SKIPPED reason=merge_not_ready`, warn, no ready labels. Alternatives: B — fall back to `--auto`. Why: B reopens the finding's window (§1). Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] How is an existing enrollment cancelled on a head change? — Picked: A — the gate job of every review run cancels any enrollment on an open `claude/*` PR, reusing its `pulls/{n}` read. Alternatives: B — cancel only in the merge helper; C — no cancellation. Why: only a clean run reaches the helper. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Add a separate cancellation when a hold is posted without a push? — Picked: A — no. Alternatives: B — an `issue_comment`-triggered workflow; C — a job in the hourly catch-all sweep. Why: after AD-1 the workflow never enrolls a `claude/*` PR (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] What if the cancellation call fails? — Picked: A — log `result=failed`, warn, continue. Alternatives: B — fail the gate job. Why: failing the gate does not stop GitHub's armed merge. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Which log keys carry the new outcomes? — Picked: A — `action=squash_sync`, `reason=merge_not_ready`, new key `AUTOFIX_CLAUDE_AUTO_MERGE_CANCELLED` registered in `agents.md`. Alternatives: B — a new key per outcome. Why: extends existing audit lines without renames (§6). Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Edit CLAUDE.md §26.H? — Picked: A — no; README and `agents.md` only. Alternatives: B — edit CLAUDE.md and its twin. Why: §5, #5316 AD-8. Applied in: no code change. Status: pending review

## Lessons

## Notes
- Security pass: skip (`security_pass_skip.py`: `ai:security` created and labelled by the issue automation).
- Phase 1 has no protected paths (no `.claude/**` edit).
- Issue base is the #5316 project branch (final PR #5323, draft into main): Activation n/a; the final-merge stage closes #5565 with `ai:merged`.
