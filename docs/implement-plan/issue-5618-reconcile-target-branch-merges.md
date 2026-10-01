# Implement-Plan Log — Mark poller-managed issues merged only on merges into their target branch

- Plan: docs/completed/issue-5618-reconcile-target-branch-merges-plan.md (moved from docs/plans/ in the completion PR)
- Source issue: shubhodeep1/coding-workflows#5618
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Base branch: claude/implement-plan-issue-4813-close-sweep-target-branch-merges
- Project branch: claude/implement-plan-issue-5618-reconcile-target-branch-merges   Final PR: #5633 draft (into claude/implement-plan-issue-4813-close-sweep-target-branch-merges)
- Status: COMPLETE
- Stage: final-merge
- Activation: n/a (base claude/implement-plan-issue-4813-close-sweep-target-branch-merges)
- Waiting on: the completion PR from claude/implement-plan-issue-5618-reconcile-target-branch-merges-complete
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: checker session_016KrwLrGKBe3mJCxcGowd7W   safety net and hand-back: see the completion stage report
- Last updated: 2026-10-01
- Last note: completion stage: validation cycle 1 passed (run 36808188447, 10/10 tests, project branch at a7803a2); plan moved to docs/completed/; conformance run 1 CONFORMANT; security skipped (plan header). Next: final-merge 1/1 marks #5633 ready and closes #5618 with `ai:merged` once it merges.

## Phases
1. [x] Phase 1 — gate every poller path that marks an issue merged (current-wave reconcile, validation-dispatch wave gate, stall-recovery `ai:merged` tag, backward-scan promotion) on the issue's target branch, with tests, README and changelog — PR #5646 merged 2026-10-01 (54f0851, merged by the operator under Q2: A, evidence https://github.com/shubhodeep1/coding-workflows/pull/5646#issuecomment-5923041874); review rounds: 2 (round 1 fixed in 8ff985c; all 26 round-2 entries rejected, the reviewers read `main`'s tree instead of the stacked base, #5824); interventions: 0

## Conformance
- Run 1 — 2026-10-01: CONFORMANT — no fixes (pre-security)

## Security pass
- Skipped (ai:security: automation-produced issue; `security_pass_skip.py` verified)

## Validation
- Run 36804574017 2026-10-01 (target_ref: claude/implement-plan-issue-5618-reconcile-target-branch-merges): failed before validating (GitHub API rate limit, HTTP 403, in "Authorize explicit validation target"); not counted as a cycle (operator Q3: A, #5504).
- Cycle 1 — run 36808188447 2026-10-01 (target_ref: claude/implement-plan-issue-5618-reconcile-target-branch-merges, head a7803a2): status=pass raw_status=pass — Runtime validation passed (10/10 tests, 274s).

## Completion
- Completion PR from claude/implement-plan-issue-5618-reconcile-target-branch-merges-complete (open) — doc moved to docs/completed/issue-5618-reconcile-target-branch-merges-plan.md
- Final PR #5633 draft

## Activation
- n/a: the base is the #4813 project branch, so this change goes live with project #4813.

## Auto-decisions
- AD-1 [plan, 2026-09-30] Which producers of merged state does the fix cover? — Picked: A — all four poller paths (current-wave reconcile loop, validation-dispatch wave gate, stall-recovery `_reconcile_merged_pr_issue`, backward-scan promotion) through one predicate. Alternatives: B — only the reconcile loop the finding names; C — paths 1 and 2. Why: paths 3 and 4 write `ai:merged`, which `check-wave-status` reads as merged; the #4813 lesson says gate every path in the same change. Applied in: phase 1 PR. Status: pending review
- AD-2 [plan, 2026-09-30] Which branches count as a wave child's target in paths 1, 2 and 4? — Picked: A — the default branch (from the PR JSON, empty fails closed) and the project's integration branch from the state file. Alternatives: B — also the child's body `Integration branch:` line. Why: the state file is authoritative; body text is editable. Applied in: phase 1 PR. Status: pending review
- AD-3 [plan, 2026-09-30] What happens to a merged candidate the rule rejects in path 1? — Picked: A — log it and try the next candidate. Alternatives: B — keep it as an unmerged fallback. Why: mirrors the sweep. Applied in: phase 1 PR. Status: pending review
- AD-4 [plan, 2026-09-30] Are unmerged candidates also gated on base? — Picked: A — no. Alternatives: B — gate every candidate. Why: `pr_state` is not a decision input downstream (§5). Applied in: no code change. Status: pending review
- AD-5 [plan, 2026-09-30] Where does the validation-dispatch gate get head repository and default branch? — Picked: A — two extra fields on the existing GraphQL PR fragment. Alternatives: B — one REST read per merged link. Why: §15. Applied in: phase 1 PR. Status: pending review
- AD-6 [plan, 2026-09-30] Where does the predicate live? — Picked: A — `scripts/orchestrate_poll_process.sh`, next to `_pr_json_is_issue_implementation_pr`. Alternatives: B — `scripts/gh_helpers.sh`. Why: every caller is in the poller. Applied in: phase 1 PR. Status: pending review
- AD-7 [plan, 2026-09-30] Is `validation_fix_issue_has_merged_pr_evidence` fixed here? — Picked: A — no; noted. Alternatives: B — add the rule there too. Why: outside the finding (§5). Applied in: no code change. Status: pending review
- AD-8 [plan, 2026-09-30] In path 3, does a rejected merged PR still skip the stall recovery? — Picked: A — yes; only the label write is gated. Alternatives: B — let the recovery run. Why: existing behaviour outside the finding. Applied in: phase 1 PR. Status: pending review
- AD-9 [plan, 2026-09-30] Where does path 3 get the base, head repository, and targets? — Picked: A — `_reconcile_merged_pr_issue` fetches `pulls/<n>` and `issues/<n>` on a merged hit. Alternatives: B — thread full PR JSON through six callers; C — accept only default-branch merges. Why: one change in the single label writer. Applied in: phase 1 PR. Status: pending review

## Lessons
- [source:plan-deviation] A finding that names one path that marks an issue merged is rarely the only one: search for every writer of the terminal label and every producer of the wave-status merged signal (`ai:merged` adds, `--pr-states-json` builders), and gate them all with one shared predicate in the same change. (files: scripts/orchestrate_poll_process.sh)
- [source:plan-deviation] Poller test fixtures that model a merged PR must set `baseRefName`: the mock `pulls/<n>` payload returns an empty base otherwise, which any target-branch rule rightly rejects. (files: tests/test_orchestrate_poll_process.py)
- [source:validation] Before trusting a read-result run, confirm its `target_ref` input in the run log: two stage sessions that dispatch `internal-validate.yml` seconds apart can each record the other's run id (#5016). (files: .claude/scripts/dispatch_workflow.py, .claude/commands/implement-plan-claude.md)

## Notes
- Security pass: skip (`security_pass_skip.py`: ai:security, created and labelled by the issue automation).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5618#issuecomment-5909984944
- Plan widened during planning (AD-1): the finding names the reconcile loop, but the #4813 project checker's context recorded that conformance run 1 had flagged `_reconcile_merged_pr_issue` too; tracing every `ai:merged` writer found four paths.
- Final PR #5633 (draft into claude/implement-plan-issue-4813-close-sweep-target-branch-merges, `Refs #5618`; the final-merge stage closes #5618 and labels it `ai:merged`).
- Out of scope, seen while tracing: `validation_fix_issue_has_merged_pr_evidence` checks the base only when a base is passed (not on the validation fix-up path) and never checks head identity (AD-7). Worth its own issue.
- 2026-10-01: phase 1 review round 2 had no valid finding and no verdict bot is configured; blocked as Q2 on the issue (comment 5922366480). Q2: A (operator, comment 5923063484): PR #5646 merged into the project branch as 54f0851.
- 2026-10-01: validation run 36804574017 hit the `GH_PAT` hourly API budget before validating; blocked as Q3 (comment 5923418086). Q3: A (comment 5923682204): re-dispatched as cycle 1/3.
- 2026-10-01: project branch synced with the base as a7803a2 (clean merge of #5617's work); already up to date at the completion stage (the base's PR #4826 was still open, so the base did not move).
- 2026-10-01: the validation stage recorded run 36808191924, which validated `claude/implement-plan-issue-5664-archived-stage-disables-recovery` (dispatched 3 s after ours). This project's run is 36808188447 (target_ref and head a7803a2 confirmed in its log). Known race: #5016.
- Protected paths: none (phase 1 touches no `.claude/**` path).
