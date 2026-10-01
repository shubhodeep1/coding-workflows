<!-- changelog: security -->
- **A flood of PR comments can no longer block a `claude/*` PR from merging.** The merge hold gate in `review_autofix.yml` now reads every comment on the PR instead of stopping after 10 pages, so a PR with more than 1,000 comments still gets a real hold decision.

`scripts/claude_merge_hold_gate.py` re-reads a `claude/*` PR's claims just before auto-merge is enabled. It used `check_in_status.gh_api_list`, which refuses after 10 pages of 100 comments, and the gate turns any read it cannot finish into a refusal. Anyone able to comment could push a PR past 1,000 comments and stop it from ever auto-merging (issue #5566, security audit finding `comment-flood-blocks-merge-gate`). The gate now streams every page until the first short one. It keeps only the trusted claim-marker lines, so memory does not grow with untrusted or large comments. It retries a page read that fails, and still fails closed when a page stays unreadable or comes back malformed. The hold decision itself is unchanged: the same trust rules, and the latest trusted claim on the head still decides.

| The numbers that matter | Value |
| --- | --- |
| Comment pages read | all (was: at most 10, then refuse) |
| API calls | 1 per 100 comments, as before |
| Attempts per page | 3 (back-off 1 s, then 2 s); a malformed page is not retried |
| Kept in memory | trusted claim-marker lines only |
| Unchanged | exit codes, JSON fields, `AUTOFIX_AUTO_MERGE_SKIPPED` / `AUTOFIX_MERGE_HOLD_GATE` log keys |

What this means for operators: a busy or spammed `claude/*` PR no longer sits forever with `AUTOFIX_AUTO_MERGE_SKIPPED … reason=gate_unavailable`. A `hold` claim still blocks the merge, however many comments sit in front of it.

### For contributors

The §26 checker and the catch-all sweep still read comments through `gh_api_list` with its 10-page cap. They retry on their next check-in instead of refusing a merge, so they are unchanged here.
