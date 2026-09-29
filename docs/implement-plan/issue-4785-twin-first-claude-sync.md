# Implement-Plan Log — Land .claude/ changes without a watched session: twin-first edits plus an Actions sync PR

- Plan: docs/plans/issue-4785-twin-first-claude-sync-plan.md
- Source issue: shubhodeep1/coding-workflows#4785
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-4785-twin-first-claude-sync   Final PR: #4804 draft
- Status: IN_PROGRESS
- Stage: conformance 2/3
- Activation: not started
- Waiting on: conformance fix PR (branch claude/implement-plan-issue-4785-twin-first-claude-sync-conformance-fix-2)
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_01S7jAxpFysQWjX6bnYiFJc4   safety net / hand-back: armed by the conformance 2/3 stage (ids in its report)
- Last updated: 2026-09-29
- Last note: conformance run 2 (project branch caa606a, main synced in with the #4948 merge): INCOMPLETE — `twin_history_blobs` skipped merge commits, so main's merge-born implement-plan-claude.md read as a conflict (the first sync PR would have held it for the owner) and 4 parity asserts failed in full clones (BLOCKER); #4948's interim twin-first default reached its sunset trigger and its tests failed (BLOCKER); AD-12. Both fixed in conformance fix PR 2.

## Phases
1. [x] Phase 1 — twin-first docs, sync workflow, sync-state checks (no `.claude/**` path is edited; protected paths: none)   — PR #4807 merged 2026-09-29 (0e42fee); review rounds: 2; interventions: 0

## Conformance
- Run 1 — 2026-09-29: INCOMPLETE (Implemented: PARTIAL after Correctness FAIL; 1 BLOCKER, 3 CONCERNs, AD-11) — conformance fix PR #5108 merged 2026-09-29 (6fe6ed1) (pre-security)
- Run 2 — 2026-09-29: INCOMPLETE (Implemented: PARTIAL after Correctness FAIL; 2 BLOCKERs, AD-12) — conformance fix PR from `claude/implement-plan-issue-4785-twin-first-claude-sync-conformance-fix-2` (pre-security)

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
- AD-11 [conformance 1/3, 2026-09-29] The shipped phase differs from the plan text in five equivalent-or-stronger ways (no `apply`/`approval` subcommands, `GUARD_PATH_PREFIXES`/`GUARD_PATH_FILES`, open sync PR matched by same-repo head not `GH_PAT` author, gate log key `AUTOFIX_GATE_SKIP reason=claude_twin_sync`, a review runs the full pass). How are they reconciled? — Picked: A — correct the plan text to the shipped behaviour. Alternatives: B — change the code to the plan text; C — record the divergence only. Why: shipped design is equivalent or stronger; B adds unused surface (§5). Applied in: conformance fix PR (plan text only). Status: pending review
- AD-12 [conformance 2/3, 2026-09-29] #4948's sunset test asserts the interim default is gone from both `.claude/commands/implement-plan-claude.md` and its twin, but this project never edits `.claude/**`, so the `.claude/` copy keeps the interim paragraph until the first sync PR after the final merge. How is the sunset guard reconciled? — Picked: A — check the twin, CLAUDE.md, agents.md, and master-session.md only; `.claude/` is covered by `test_template_parity` (`assert_claude_not_ahead`) and catches up through the sync PR. Alternatives: B — keep the `.claude/` asserts, which fail on the project branch and on `main` until the sync PR merges; C — edit `.claude/` in this PR, which the plan's non-goals and CLAUDE.md §28.C forbid. Why: the plan's G8 / AD-7 rule that text tests load the twin (§5). Applied in: conformance fix PR 2. Status: pending review

## Lessons
- [source:plan-deviation] A CI check over a commit range must only run on ranges that are one PR's net change; a promotion range (main → stable) can hold a sync followed by a newer source change and read as drift. (files: .github/workflows/ci.yml, scripts/claude_twin_sync.py)
- [source:intervention] A driver that updates an existing PR's head branch must first confirm the branch still exists on origin and close the PR when it does not; otherwise a deleted branch turns every later scheduled run into the same failed fetch. (files: scripts/claude_twin_sync.py)
- [source:intervention] A new workflow or `scripts/` file must be listed in docs/INVENTORY.md in the same PR; `tests/inventory_parity.py` fails CI otherwise, even when the plan's docs step does not name the inventory. (files: docs/INVENTORY.md, tests/inventory_parity.py)
- [source:conformance] Merging the default branch into a project branch can bring in tests written under the invariant the project replaces (here a `.claude/` ↔ twin byte-parity assert from #4787); rerun the affected suites after every sync merge and convert them in the next PR. (files: tests/test_check_in_session_targeting.py, tests/claude_twin_state.py)
- [source:conformance] A command twin ships to consumer repos, so a rule that names `workflow-templates/.claude/` or a coding-workflows-only workflow must also say what a consumer does. (files: workflow-templates/.claude/commands/fix-claude-pr.md, workflow-templates/.claude/commands/implement-issue-claude.md)
- [source:conformance] A scheduled job that posts a commit status must post it only when it changes: GitHub refuses more than 1000 statuses per sha and context. (files: scripts/claude_twin_sync.py)
- [source:conformance] A git-history lookup that asks "was this blob ever a version of the file" must include merge commits (`git log -m --raw`): a merge that combines two edits produces a version no single-parent commit holds, and project-branch syncs make such merges routine. (files: scripts/claude_twin_sync.py, tests/claude_twin_state.py)

## Notes
- Security pass: run (`security_pass_skip.py` → `{"skip": false, "reason": "no skip label"}`).
- Permission mode auto; session started by the Claude issue dispatcher (trigger `dispatch shubhodeep1/coding-workflows#4785: start`).
- Plan deviation (phase 1): the CI sync-state step enforces only on PRs into `main` and pushes to `main`; `stable` promotion ranges are skipped (see the lesson). The sync PR body names no issue (no `Refs #4785`), and non-guard sync heads get a `success` `claude-twin-sync/owner-approval` status. The plan text was corrected in the same commit.
- Conformance 1/3 (2026-09-29): merged origin/main (aebaa17) into the project branch cleanly (d33ed5c) before the audit.
- Issue progress comment id 5868368043. `CLAUDE_FIXER_HANDOFF_AUTHOR_LOGIN` = `shubhodeep1`; no verdict bot configured.
- Conformance 2/3 (2026-09-29): merged origin/main (098f1db, with #4948's final PR #4989) into the project branch (caa606a); agents.md conflicted and kept the project side, since #4948's own sunset says #4785 removes its interim sentence. Local checks on the fix: the 22 twin-related suites pass (1365 tests; 6 failed before the fix), and `plan` on a local merge of the fix into main copies the three commands with no conflict and no owner gate.
