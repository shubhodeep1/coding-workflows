# Implement-Plan Log — Never continue a Claude fix on stale guards after a Claude-asset sync conflict

- Plan: docs/plans/issue-5258-asset-sync-outside-conflict-fail-closed-plan.md
- Source issue: shubhodeep1/coding-workflows#5258 (https://github.com/shubhodeep1/coding-workflows/issues/5258)
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: claude/implement-plan-issue-4952-sync-claude-assets-at-session-start
- Project branch: claude/implement-plan-issue-5258-asset-sync-outside-conflict-fail-closed   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from the issue base; phase 1 starting (twin-first)

## Phases
1. [ ] Phase 1 — resolve or fail closed on an outside-`.claude/` Claude-asset sync conflict — protected paths: .claude/commands/fix-claude-pr.md, .claude/commands/implement-plan-claude.md

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue) — verified by .claude/scripts/security_pass_skip.py on 2026-09-29

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] How should a Claude fix handle a Claude-asset sync merge that conflicts only outside `.claude/`? — Picked: A — resolve the conflict inside the sync merge under the caller's §12 conflict rules, and when the resolution is not evident, `git merge --abort` and stop like the caller's unresolvable conflict (fail closed); never continue on the unsynced head. Alternatives: B — always fail closed: abort, hold, and wait for a human; C — overlay the default branch's `.claude/hooks` and `settings.json` on the working tree and continue on the unsynced head. Why: current guards run through the whole fix without parking every routine conflict PR on a human or slipping unreviewed guard files into the fix. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-29] Should the rule cover only `/fix-claude-pr` or every PR-head caller of the Claude-asset sync? — Picked: A — every PR-head caller, as one shared rule in Claude-asset sync step 5. Alternatives: B — `/fix-claude-pr` only. Why: `/implement-plan-claude` steps 7 and 7a have the same gap in the same flow. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-29] Which commit subject does a sync merge that resolved a conflict carry? — Picked: A — the sync's documented `[claude-asset-sync] merge <source> for .claude/ guard updates` (kept by `git commit --no-edit`). Alternatives: B — `[claude-merge-resolve] merge <source>`. Why: one documented subject; both break the consecutive `[claude-autofix]` count the same way. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-29] Should the phase also stop fixes on branches the sync skips (step 3: a base other than the default branch)? — Picked: A — no; non-goal. Alternatives: B — fail closed on every skipped sync too. Why: the finding names the aborted-merge path only, and B would stop every fix on a `stable`- or PR-based branch. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-29] What happens to the report phrase `claude_assets=stale (conflict outside .claude/)`? — Picked: A — drop it. Alternatives: B — keep it documented as an alias. Why: the state no longer occurs, it never reached the default branch, and nothing parses it. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Issue mode: base branch `claude/implement-plan-issue-4952-sync-claude-assets-at-session-start` (open draft final PR #4995) is not the default branch, so the final-merge stage closes #5258 explicitly with `ai:merged`, and activation is `n/a (base …)` unless the base moves onto `main`.
- Protected-path approval: phase 1 — twin-first (automatic, interim until #4785) (2026-09-29)
