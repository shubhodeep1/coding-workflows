<!-- changelog: security -->
- **A clean Claude-fixer review that is still waiting on CI no longer merges after the PR is retargeted.** The pending-checks marker from issue #4900 is now bound to the base the review ran against, and a changed base gets a fresh review.

The `claude-pr-catch-all` sweep enables auto-merge for a `claude/*` PR whose reviewers finished clean before its checks did. Until now the marker it trusts, `ai:claude-fixer-pending-checks:v1`, named only the reviewed head. A PR author could retarget the PR after the clean review, for example from its project branch to `main`, without pushing, and the sweep would enable auto-merge into a base whose diff nobody reviewed (security finding #5147). The review workflow now adds `<!-- ai:claude-fixer-pending-checks:v2 head=<sha> round=<n> ledger=<sha256> base_sha=<sha> base_ref_sha256=<sha256> -->` and a `Reviewed base:` line, taken from the review run's own PR snapshot. `scripts/claude_fixer_pending_checks.py` refuses to merge unless the PR's current `base.sha` and `base.ref` match that line, and logs `pending_checks ... state=base_changed` (or `base_unbound` for a comment without the line). The review gate stops skipping dispatched re-runs on such a head, so the next 30-minute run of `review_autofix_sweep.yml` reviews the PR again against its new base, and that clean review posts a new, rebound marker.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls | 0 (the gate and the sweep already read the PR) |
| Delay before the fresh review of a retargeted PR | up to 30 minutes (the review sweep's `*/30 * * * *` tick) |
| Extra reviewer runs when the base branch only gains commits | 0 (the PR's `base.sha` changes on a retarget or a PR sync, not on every push to the base) |

What this means for operators: nothing to configure. A retargeted `claude/*` PR that was waiting on CI is reviewed again instead of merged; a PR that keeps its base merges exactly as before. A review run whose PR snapshot has no valid base posts the ordinary findings hand-off instead of a pending-checks comment.

### For contributors

The base ref enters the marker only as its sha256, because a git ref name may contain `-->`. The v1 line is unchanged and still posted; both readers (`gate_claude_pending_checks_on_head` in `.github/workflows/review_autofix.yml` and `find_pending_marker` / `evaluate`) require the bound v2 line. `tests/test_claude_fixer_pending_checks.py` replays the retarget end to end, and `tests/test_review_autofix_claude_fixer_mode.py` covers the hand-off step's v2 line and the gate.
