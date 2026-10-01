<!-- changelog: security -->
- **A Claude-fixer PR retargeted while the sweep is enabling its auto-merge no longer merges into the new base.** The merge helper now re-checks the reviewed base right before the merge call, and the sweep disables auto-merge again if the base or head moved around that call.

The `claude-pr-catch-all` sweep enables auto-merge for a `claude/*` PR whose reviewers finished clean before its checks did. Issue #5147 bound that decision to the reviewed base, but the sweep checked the base on its first PR read. The merge came minutes and a dozen API calls later, and `scripts/review_enable_auto_merge.sh` re-checked only the head before `gh pr merge --auto`. A PR retargeted in between could still auto-merge into a base nobody reviewed (security finding #5905).

The sweep now passes the reviewed base to the helper as `REVIEWED_BASE_REF` / `REVIEWED_BASE_SHA`. The helper refuses a mismatch from the PR read it already makes right before the merge call, printing `AUTOFIX_AUTO_MERGE_HEAD_BOUND ... action=refuse reason=base_changed`. GitHub's merge APIs bind only the head, so the sweep then reads the PR once more:

- a moved head or base, or a failed read, runs `gh pr merge --disable-auto` and logs `pending_checks ... state=merge_revoked`, after one more read confirms the PR did not merge first;
- a PR that already merged with another head or base ref, or merged that way before the disable landed, logs `merged_unreviewed_base` (a merge with the reviewed head and base ref stands);
- a confirming read that fails logs `merge_revoke_unconfirmed`;
- those two and a failed disable (`merge_revoke_failed`) also print a `::warning::` line.

| The numbers that matter | Value |
| --- | --- |
| New API calls for the merge-time base check | 0 (it reuses the helper's existing PR read) |
| New API calls per merge the sweep enables | 1 PR read, plus 1 auto-merge disable and 1 confirming PR read only when the head or base moved |
| Delay before a revoked PR is reviewed again | up to 30 minutes (the review sweep's `*/30 * * * *` tick, through the #5147 gate rule) |

What this means for operators: nothing to configure. A `claude/*` PR that keeps its base merges exactly as before. A `merge_revoke_failed`, `merge_revoke_unconfirmed`, or `merged_unreviewed_base` warning in the `claude-pr-catch-all` log means a PR may have merged, or may still merge, into a base its review did not cover; check that PR by hand.

### For contributors

The new helper inputs are optional, and the workflow's own `Enable auto-merge on PR` step sets neither, so its behaviour is unchanged. Setting either input means both must be valid and match, so a partial input fails closed. A merged PR is compared by head and base ref only, because a merge can refresh its `base.sha` snapshot. `tests/test_claude_fixer_pending_checks.py` retargets or merges the PR between each of its PR reads (up to four) with a sequenced fake `gh`.
