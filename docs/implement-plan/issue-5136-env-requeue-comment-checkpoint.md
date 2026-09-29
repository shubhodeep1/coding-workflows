# Implement-Plan Log — Checkpoint the environment re-queue comment scan so a comment flood cannot disable recovery

- Plan: docs/plans/issue-5136-env-requeue-comment-checkpoint-plan.md
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Source issue: shubhodeep1/coding-workflows#5136   Base branch: claude/implement-plan-issue-4938-environment-blocker-self-heal
- Project branch: claude/implement-plan-issue-5136-env-requeue-comment-checkpoint   Final PR: #5162 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #5177
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01XSaY4YY6PftJzkPwTGsWh1 (reused; safety net and hand-back ids in the stage report)
- Last updated: 2026-09-29
- Last note: review round 1 on PR #5177: 1 finding, fixed (bulk deletion stalled the step-back); waiting on the next review round or merge

## Phases
1. [ ] Phase 1 — checkpointed comment scan, wired into the watchdog   — PR #5177 open (waiting); review rounds: 1; interventions: 0

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
- AD-6 [phase 1/1 — review round, 2026-09-29] The `env_requeue_plan` batching-contract docstring block this PR extends uses tab-plus-space alignment, which ruff E101 flags in `ci.yml`'s lint (already red on the #4938 base with 24 such lines). How should this PR handle it? — Picked: A — reformat only that block (the lines this PR edits) to tabs only, and leave the 13 lines in `env_requeue_decision` to the #4938 project. Alternatives: B — keep the block's style and add 5 more E101 lines; C — also reformat `env_requeue_decision`, which this PR does not touch. Why: B keeps PR #5000's lint red after #4938 fixes its own lines; C widens the PR into code it does not change (§5). Applied in: PR #5177. Status: pending review

## Lessons
- [source:plan-deviation] A resumable page scan that re-reads its cursor page needs at least two reads per run, or a full cursor page consumes the whole budget and the scan never advances. (files: scripts/claude_issue_route.py)
- [source:intervention] A resumable scan that steps back to find its cursor must save each step back in the checkpoint, or a deletion larger than one run's budget makes every run repeat the same reads and the scan never finishes. (files: scripts/claude_issue_route.py)

## Notes
- Issue mode: the plan was written by /implement-issue-claude from #5136 (progress comment 5890575522).
- Protected paths: none (phase 1 touches no `.claude/**` file).
- Base-branch lint: `ci.yml`'s `ruff check --select E,F --ignore E501 scripts/*.py` fails on the #4938 base (PR #5000, run 36563232033) with E101 in the `env_requeue_decision` docstring; that is the #4938 project's to fix (AD-6).
- Local test runs need Python 3.12 (CI's version): `scripts/workflow_retro.py` does not parse on 3.11.
- This session had no `mcp__github__*` tools; GitHub writes go through `gh api` calls the CLAUDE.md §23.H guard classifies as routine.
