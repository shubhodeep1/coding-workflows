<!-- changelog: security -->
- **review_autofix now merges only the head it evaluated.** Every auto-merge call owned by the workflow, including deterministic skip, the clean-review helper, and the review-blocked judge, passes `--match-head-commit` so a later push cannot merge under an earlier decision.

On 2026-09-09 and 2026-09-16 two PRs in shubhodeep1/fun-token-multi-chain (#498 and #527) merged into `main` with `ai:review-skipped` and no reviewer run. Each was opened with a diff under the 10/10 line threshold, the gate approved the deterministic skip, and a code push that landed 13 to 19 seconds later was what `gh pr merge --squash --auto` shipped, because the merge was issued by PR number with no head binding. The second one auto-deployed the session-server and the lobby Worker. The `synchronize` run for the real diff correctly refused the skip but found the PR already closed. The workflow now exports the gate's head SHA as the `head_sha` output, `deterministic-skip-merge` binds its merge to it, and `scripts/review_enable_auto_merge.sh` binds the reviewed-path merge through the existing `INITIAL_HEAD_SHA` input. Writable heads refresh `INITIAL_HEAD_SHA` after fetching their live branch; read-only paths retain the commit checked out before their early exit. An unknown or malformed SHA refuses the merge with a `::warning::` rather than issuing it unbound, matching `scripts/review_rb_judge.sh`.

| The numbers that matter | Value |
| --- | --- |
| Merge call sites now head-bound | 9 (`review_autofix.yml` ×2, `review_enable_auto_merge.sh` ×2, `review_rb_judge.sh` ×5) |
| Incident PRs | shubhodeep1/fun-token-multi-chain#498 (+117/−4 merged, +2/−2 evaluated), #527 (+324/−16 merged, +4/−3 evaluated) |
| Tracking issue | #4109 |

What this means for consumer repos: nothing to configure. The fix arrives with the next `@stable` sync of `review_autofix.yml`. A PR whose head moves after the gate ran now stays open with a `Could not enable auto-merge` warning naming the moved head, its linked issues do not receive `ai:ready-to-merge`, and its next `synchronize` event re-evaluates the new diff. `ai:review-skipped` is still applied by the skip job before the merge attempt.

### For contributors

Read-only review paths capture `INITIAL_HEAD_SHA` before their early exit, while the review-blocked judge embeds and binds to its checked-out `RB_JUDGED_HEAD_SHA`. Audit lines: `AUTOFIX_DET_SKIP_MERGE_BOUND pr=<n> head_sha=<sha> action=<squash|merge_commit|refuse>` (skip job) and `AUTOFIX_AUTO_MERGE_HEAD_BOUND ...` (helper). Residual not covered here: when `--auto` enrols instead of merging immediately, the binding covers the enrolment only; GitHub keeps auto-merge enabled across later pushes by write-access users. That is tracked separately in #4109.
