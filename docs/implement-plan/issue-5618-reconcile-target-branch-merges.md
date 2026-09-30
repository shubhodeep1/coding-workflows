# Implement-Plan Log — Mark poller-managed issues merged only on merges into their target branch

- Plan: docs/plans/issue-5618-reconcile-target-branch-merges-plan.md
- Source issue: shubhodeep1/coding-workflows#5618
- Repo: shubhodeep1/coding-workflows   Default branch: main
- Project branch: claude/implement-plan-issue-5618-reconcile-target-branch-merges   Final PR: pending
- Status: IN_PROGRESS
- Stage: phase 1/1
- Activation: not started
- Waiting on: none
- Stage model: claude-opus-5-5   Permission mode: auto
- Check-in: none
- Last updated: 2026-09-30
- Last note: project branch opened from claude/implement-plan-issue-4813-close-sweep-target-branch-merges; phase 1 starting.

## Phases
1. [ ] Phase 1 — gate every poller path that marks an issue merged (current-wave reconcile, validation-dispatch wave gate, stall-recovery `ai:merged` tag, backward-scan promotion) on the issue's target branch, with tests, README and changelog

## Conformance

## Security pass

## Validation

## Completion

## Activation

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

## Notes
- Security pass: skip (`security_pass_skip.py`: ai:security, created and labelled by the issue automation).
- Progress comment: https://github.com/shubhodeep1/coding-workflows/issues/5618#issuecomment-5909984944
- Out of scope, seen while tracing: `validation_fix_issue_has_merged_pr_evidence` checks the base only when a base is passed (not on the validation fix-up path) and never checks head identity (AD-7). Worth its own issue.
