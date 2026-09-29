# Implement-Plan Log — Land .claude/ changes without a watched session: twin-first edits plus an Actions sync PR

- Plan: docs/plans/issue-4785-twin-first-claude-sync-plan.md
- Source issue: shubhodeep1/coding-workflows#4785
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4785-twin-first-claude-sync   Final PR: #4804 draft
- Status: IN_PROGRESS
- Stage: phase 1/1 — review round
- Activation: not started
- Waiting on: PR #4807
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01S7jAxpFysQWjX6bnYiFJc4   safety net trig_01RN4GreEVToDoVRMbvoVG4V   hand-back trig_01VxHtC3i2H4meVdBzzkwGtr
- Last updated: 2026-09-29
- Last note: review round 1 (head 070bf5fd806f): all 5 consensus findings valid and fixed in one [claude-autofix] commit (orphaned sync PR on a deleted head branch, label-create errors, violation messages, dead constant, upstream-only twin-state exemption); waiting on round 2.

## Phases
1. [ ] Phase 1 — twin-first docs, sync workflow, sync-state checks (no `.claude/**` path is edited; protected paths: none)   — PR #4807 open (waiting); review rounds: 1; interventions: 0

## Conformance

## Security pass

## Validation

## Completion

## Activation

## Auto-decisions
- AD-1 [plan, 2026-09-28] Which files does the sync treat as twins, given five intentional consumer-variant commands and one upstream-only command? — Picked: A — an explicit `UPSTREAM_ONLY_PATHS` list in `scripts/claude_twin_sync.py`; never synced, exempt from the sync-state check, protected-path stop kept for them. Alternatives: B — sync every differing file; C — make the variants identical. Why: keeps both editions intact (§5). Applied in: phase 1. Status: pending review
- AD-2 [plan, 2026-09-28] How is "merges only after an owner approving review" enforced when GH_PAT authors PRs as the owner and `main` has no required reviews? — Picked: A — the workflow never approves, merges, or enables auto-merge on a guard/conflict sync PR; it posts `claude-twin-sync/owner-approval` from a verified owner APPROVED review on the current head, and the owner merges. Alternatives: B — enable auto-merge once approval verified; C — open guard PRs with github.token. Why: literal "never approves or merges it itself", fewest privileges (§1). Applied in: phase 1. Status: pending review
- AD-3 [plan, 2026-09-28] How do sync PRs stay out of Claude-fixer review, auto-merge, and the §26.H sweep? — Picked: A — `[skip ai]` in the body plus a head-ref skip in the review_autofix gate and claude_pr_sweep.py. Alternatives: B — marker only; C — head ref only. Why: extends the existing mechanism; the head ref cannot be edited. Applied in: phase 1. Status: pending review
- AD-4 [plan, 2026-09-28] What does "auto-merges once required checks pass" mean with no required checks on `main`? — Picked: A — the workflow merges a non-guard sync PR itself (`--squash --match-head-commit`) only when every head check run completed green including CI `lint` and `merge-check` passes; retried on CI completion and hourly. Alternatives: B — `gh pr merge --auto` (merges before CI). Why: B would merge untested. Applied in: phase 1. Status: pending review
- AD-5 [plan, 2026-09-28] Conflicts-only case (nothing to copy) — Picked: A — open/update the sync PR with an empty marker commit, the conflict list, and the approval label. Alternatives: B — alert only, no PR. Why: follows the issue literally. Applied in: phase 1. Status: pending review
- AD-6 [plan, 2026-09-28] Who alerts the operator for `ai:claude-sync-approval`? — Picked: A — the workflow sends one Telegram alert per new head via scripts/tg_helpers.sh; pickup unchanged. Alternatives: B — edit the upstream-only claude-issue-pickup.md. Why: no protected-path stop. Applied in: phase 1. Status: pending review
- AD-7 [plan, 2026-09-28] How do suites cover the twin on a twin-only PR? — Picked: A — behaviour/text tests load the twin; parity asserts become `assert_claude_not_ahead` (skips in shallow clones where the CI `check` step enforces). Alternatives: B — parametrise over both copies. Why: the twin is the source of truth. Applied in: phase 1. Status: pending review
- AD-8 [plan, 2026-09-28] How is an open sync PR updated under the all-branch non-fast-forward rule? — Picked: A — a forward-only two-parent commit via `git commit-tree`. Alternatives: B — close and reopen each run. Why: B duplicates PRs. Applied in: phase 1. Status: pending review
- AD-9 [plan, 2026-09-28] What is the "old twin" in the conflict rule for a catch-up run? — Picked: A — any version in the twin path's git log (≤ 500 revisions). Alternatives: B — only the push's `before` twin. Why: same answer for every trigger. Applied in: phase 1. Status: pending review
- AD-10 [plan, 2026-09-28] What happens to an open sync PR when the twins already match? — Picked: A — close it with a comment. Alternatives: B — leave it open. Why: avoids stale PRs. Applied in: phase 1. Status: pending review

## Lessons
- [source:plan-deviation] A CI check over a commit range must only run on ranges that are one PR's net change; a promotion range (main → stable) can hold a sync followed by a newer source change and read as drift. (files: .github/workflows/ci.yml, scripts/claude_twin_sync.py)
- [source:intervention] A driver that updates an existing PR's head branch must first confirm the branch still exists on origin and close the PR when it does not; otherwise a deleted branch turns every later scheduled run into the same failed fetch. (files: scripts/claude_twin_sync.py)

## Notes
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Permission mode auto; session started by the Claude issue dispatcher (trigger `dispatch shubhodeep1/coding-workflows#4785: start`).
- Plan deviation (phase 1): the CI sync-state step enforces only on PRs into `main` and pushes to `main`; `stable` promotion ranges are skipped (see the lesson). The sync PR body names no issue (no `Refs #4785`), and non-guard sync heads get a `success` `claude-twin-sync/owner-approval` status. The plan text was corrected in the same commit.
- Issue progress comment id 5868368043. `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` = `shubhodeep1`; no verdict bot configured.
