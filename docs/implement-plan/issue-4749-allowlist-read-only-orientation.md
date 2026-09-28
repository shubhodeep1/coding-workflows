# Implement-Plan Log — Allowlist the read-only orientation reads so a classifier outage cannot deny them

- Plan: docs/plans/issue-4749-allowlist-read-only-orientation-plan.md
- Source issue: shubhodeep1/coding-workflows#4749
- Repo: shubhodeep1/coding-workflows   Default branch: main   Base branch: main
- Project branch: claude/implement-plan-issue-4749-allowlist-read-only-orientation   Final PR: draft (opened right after this commit; its number is in the issue's `ai:claude-blocked` comment and the next phase PR's log)
- Status: BLOCKED
- Stage: phase 1/1
- Activation: not started
- Waiting on: none (protected-path approval for phase 1, asked on #4749)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none (no wait armed while blocked)
- Last updated: 2026-09-28
- Last note: phase 1 edits the root `.claude/settings.json`, a protected path (CLAUDE.md §28.C). The phase stopped before it started and the approval question is on #4749.

## Phases
1. [ ] Phase 1 — allowlist `git branch --show-current`, `ls *`, `head *` (.claude/settings.json, workflow-templates/.claude/settings.json, tests/test_permission_prompts.py, agents.md, changelog.d/4749-allowlist-read-only-orientation.md) — protected paths: .claude/settings.json

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Is this prompt by design, so the issue closes as not planned? — Picked: A — no, fix it. Every subcommand is a read, and the denial came from a classifier outage that an allow rule makes irrelevant. Alternatives: B — close as not planned because the outage was transient. Why: the same outage would deny the next stage's orientation read too. Closing an issue this session did not open is also a §23.C ask-first operation. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-28] Where does the fix live? — Picked: A — exact `permissions.allow` rules in `.claude/settings.json` and its template twin (fix step 3 in the issue). Alternatives: B — a CLAUDE.md rule to use Glob and Read instead of `ls | head`; C — extend a `PreToolUse` hook to approve read-only compounds. Why: A is deterministic and the smallest change (§5). B depends on the model and leaves `git branch` uncovered. C is larger and also edits `.claude/**`. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-28] Which rules? — Picked: A — `Bash(git branch --show-current)` (exact), `Bash(ls *)`, `Bash(head *)`. Alternatives: B — exact pipe forms only, such as `Bash(head -50)`; C — A plus `permissions.blockReadsOutsideWorkingDirectories`. Why: A covers the pattern without approving any write. B only fixes one line count. C changes read behaviour for every session, beyond this issue (§5). Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-28] Fold the sibling permission-prompt issues (#4751, #4759, #4760, #4761, #4767) into this project? — Picked: A — no, each keeps its own issue-mode project. Alternatives: B — fold them in. Why: `/implement-issue-claude` runs one issue per chain. Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-28] Mirror the rules into `workflow-templates/.claude/settings.json`? — Picked: A — yes, keep the two files byte-identical. Alternatives: B — root file only. Why: consumer sessions hit the same classifier review, and the two files are identical today. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-28] Does the change need a `changelog.d/` fragment? — Picked: A — yes, `changed`. Alternatives: B — none. Why: §20.A requires one when a change alters what consumer repos receive on the next `@stable` sync. Applied in: phase 1 PR. Status: pending review

## Lessons

## Notes
- Invoking session: session_01NYuZyYGAWVpD3fFwZb2iKC (started by the Claude issue dispatcher routine trig_01NCKw4g5xF26G1ZXnT5VaUE, permission mode auto).
- Security pass: `security_pass_skip.py` printed `{"skip": false, "label": null, "reason": "no skip label"}`, so the pass runs.
- Issue progress comment id: 5866531584.
- Stale Routine sweep (2026-09-28): nothing to delete (kept 30, not ours 70).
- Phase 1 is blocked on the protected-path question (CLAUDE.md §28.C). A human answers it on #4749 and comments `/reclarify`, and the resumed session records the answer as `Protected-path approval: phase 1 — <letter> (<date>)` here.
