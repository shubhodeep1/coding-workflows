<!-- changelog: changed -->
- **A merge-conflict resolution on an approved head is reviewed on the resolution only.** When the conflict resolver's `[ai-merge-resolve]` commit lands on a head that review already approved, the next review run gives the reviewers only what the resolution changed (`git show --remerge-diff`) as their focus diff, instead of the whole PR (Refs #10097, Phase 3 of `docs/plans/merge-lease-short-critical-section-plan.md`).

The reviewer panel, the editor, the head-gate status and the whole-PR context are unchanged. The narrow review needs a pipeline marker comment, a merge with exactly the approved head and the base tip as parents, the pipeline commit identity, a pipeline head-gate success on the approved head and a base parent that is on the base branch. Any failed check gives the full review. The security pass audits the head as a delta since the approved head. When that head already passed a full final CI run, the resolution head gets a `[sync-quick]` final CI run. Any commit on top of the resolution gets the full review and the full run.

What this means for operators: expect `MERGE_RESOLVE_MARKER`, `REVIEW_HEAD_GATE mode=merge_resolve`, `REVIEW_DIFF_CONTEXT range=remerge` and `MERGE_RESOLVE_FINAL_CI` lines in review runs that resolved a conflict. Set the repository variable `MERGE_RESOLVE_DELTA_REVIEW_ENABLED=false` to go back to a full review.

### For contributors

New: `scripts/review_head_gate.sh merge-resolve`, `_pr_merge_resolve_marker_post` and `_pr_merge_resolve_final_ci_quick` in `scripts/pr_checks_lib.sh`, the gate output `review_remerge_head`, and the `merge_resolve_audit` carry-skip reason in `scripts/review_single_issue_security_pass.sh`.
