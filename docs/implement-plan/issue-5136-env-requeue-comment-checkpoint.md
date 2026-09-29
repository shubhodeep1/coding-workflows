# Implement-Plan Log — Checkpoint the environment re-queue comment scan so a comment flood cannot disable recovery

- Plan: docs/plans/issue-5136-env-requeue-comment-checkpoint-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#5136   Base branch: claude/implement-plan-issue-4938-environment-blocker-self-heal
- Project branch: claude/implement-plan-issue-5136-env-requeue-comment-checkpoint   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-29
- Last note: project branch opened from the issue base; phase 1 in progress

## Phases
1. [ ] Phase 1 — checkpointed comment scan, wired into the watchdog

## Conformance

## Security pass
- Skipped (ai:security: automation-produced issue, verified by .claude/scripts/security_pass_skip.py)

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-29] Where should the checkpoint persist between hourly runs? — Picked: A — the Actions cache in the watchdog workflow (restore the latest `claude-env-requeue-checkpoint-` entry, save a new one per run). Alternatives: B — a hidden checkpoint comment on each issue; C — a commit to a state branch. Why: no new permission or write surface; B needs a thread read to find it, the very thing that fails; C needs `contents: write`. Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-29] How should pagination resume safely? — Picked: A — a page cursor (page and last comment id) that re-reads the cursor page and steps back when earlier deletions moved page boundaries. Alternatives: B — a `since=` timestamp cursor; C — remove the cap and read the whole thread every run. Why: B lets edited old comments and id ordering skip comments across a partial read; C makes a flood cost unbounded API calls every hour (§15). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-29] What should the checkpoint store? — Picked: A — only trusted, decision-relevant comments (markers and trusted `/reclarify`) with the fields `env_requeue_decision` reads, bodies trimmed to 256 characters after the marker's leading whitespace. Alternatives: B — every comment. Why: the decision provably ignores every other comment, and B grows without bound. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-29] How is a scan still in progress reported? — Picked: A — a new additive `pending` list in the plan JSON, logged as `env_requeue_scan_pending`, with no action. Alternatives: B — keep reporting it under `errors` (`env_requeue_read_failed`). Why: it is expected progress, not a failure, and the existing keys stay unchanged (§6). Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-29] What does `env-requeue-plan` do without `--checkpoint`? — Picked: A — exactly today's stateless read and cap error. Alternatives: B — an in-memory checkpoint that reports `pending`. Why: backward compatibility for any other caller and the existing tests (§6); the watchdog always passes the flag. Applied in: phase 1. Status: pending review

## Lessons

## Notes
- Issue mode: the plan was written by /implement-issue-claude from #5136 (progress comment 5890575522).
- Protected paths: none (phase 1 touches no `.claude/**` file).
- This session had no `mcp__github__*` tools; GitHub writes go through `gh api` calls the CLAUDE.md §23.H guard classifies as routine.
