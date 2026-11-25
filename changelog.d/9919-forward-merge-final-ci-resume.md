<!-- changelog: fixed -->
- **A forward-merge fallback PR whose final CI passes late is now merged with a merge commit, and a stable/main divergence raises an alert.** Refs #9891, #9860 and promote run 44966377149.

On 2026-11-21 a stable-only fix (#9860) left `stable` ahead of `main`. The forward-merge fallback PR #9891 passed review, but the review path's required-checks wait timed out, and the final-CI merge resume (`_mt_final_ci_merge_resume` in `scripts/review_merge_train.sh`) dropped every `auto/forward-merge-stable-*` head as `not_eligible`. Nothing merged the PR, and the 00:00 UTC promote cycle skipped with `reason=base_not_ancestor ... ref=heads/stable` without an alert.

- The resume tick now runs forward-merge fallback heads through all its gates (hold labels, the pipeline's marker on the head, head-gate success, green required checks and named CI, base freshness and the merge lease). It enables `gh pr merge --merge --auto --match-head-commit <head>` for them (a merge commit keeps stable's ancestry) and `--squash` for every other head. The success line and the `auto_merge_refused` warning end with `method=squash|merge`. When `FORWARD_MERGE_FALLBACK_AUTO_MERGE` is not `true`, the tick drops the head with the new reason `forward_merge_auto_merge_disabled`. Both release steps (`orchestrate_poll.yml`, `cancel_on_pr_close.yml`) now pass that variable (default `true`), and the script defaults it to `true` as well.
- On a `base_not_ancestor` skip on the stable branch, `scripts/promote_main_cycle.sh` reads the open PRs into `main` once, prints `PROMOTE_CYCLE_FORWARD_MERGE_PENDING prs=<n,...|none|lookup_failed>` and sends one best-effort Telegram WARNING. The alert names same-repo forward-merge PRs only and reminds the operator to merge with "Create a merge commit". The `PROMOTE_CYCLE_SKIPPED` line, the outcome output and the exit code are unchanged.

| The numbers that matter | Value |
| --- | --- |
| New API calls in the resume tick | 0 (same merge call, different flag) |
| New API calls in the promote cycle | at most 1 read, only on that skip |
| New env vars | 0 |

What this means for consumer repos: after the next `@stable` sync, forward-merge fallback PRs labelled `ai:final-ci-merge-pending` are merged by the poll tick once their CI is green, unless `FORWARD_MERGE_FALLBACK_AUTO_MERGE` is set to something other than `true`.
