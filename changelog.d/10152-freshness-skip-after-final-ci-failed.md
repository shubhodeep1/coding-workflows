<!-- changelog: fixed -->
- **A pull request whose final CI failed on a branch-update head goes back to review instead of being approved again.** Four pull requests were stuck with a red `ci/final` and a green `ai-review/head-gate` on 2026-11-28.

When the pipeline updates an approved branch from `main`, the next review run skips the reviewers for that update head (`reason=freshness_sync`). If the full final CI then failed on that head, `ci.yml`'s `final-ci-failed` job posted its hand-back comment, but `scripts/review_head_gate.sh freshness-sync` only looked for the older `ai:freshness-ci-failed:v1` marker. Every later review run skipped the head again, set `ai-review/head-gate` to success and put the pull request back in the merge-lease queue, while nothing looked at the failing checks. The verifier now also reads the `ai:final-ci-failed:v1` and `ai:final-ci-repair-request:v1` markers for the head and sends the head to a normal review, so autofix sees the failing checks.

| The numbers that matter | Value |
| --- | --- |
| Pull requests in this state on 2026-11-28 05:30 UTC | 4 (#10152, #10133, #10070, #9902) |
| Longest time in this state | #9902: final CI failed 2026-11-27 23:28 UTC, approved again 4 times by 05:27 UTC |
| New log value | `REVIEW_HEAD_GATE mode=freshness_sync ... outcome=review reason=final_ci_failed_on_head` |
| Added API calls | 0 (the check reads the gate's existing marker-comments file) |

What this means for operators: a pull request that shows `ci/final` failure together with `ai-review/head-gate` success and `ai:merge-lease-wait` after a "Branch updated from `main`" comment should now get a normal review run on its next dispatch. The marker is trusted from `github-actions[bot]` or the `GH_PAT` login, the same rule the gate's doc-only and small-diff check already uses.
