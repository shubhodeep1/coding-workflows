<!-- changelog: security -->
- **A closed issue can no longer be resumed by replaying an old `/reclarify`.** The final-merge resume route (#5222) now runs only for the one `/reclarify` comment that triggered it, and only when that comment was posted after the issue closed.

Security finding #5514 showed that `final_merge_resume` in `scripts/claude_issue_route.py` accepted any trusted `/reclarify` after the blocked comment, including one posted before the final PR's merge closed the issue. Anyone with write access on the repo could then queue a new Claude session for the closed issue with an `opened` or `manual` intake payload, or by re-running an old clarify run. Now clarify's `Claude issue handoff` step sends the triggering comment's ID in the `claude_issue.v1` payload as `reclarify_comment_id`. The intake (`scripts/claude_issue_intake.sh`) resumes a closed issue only for a `trigger: reclarify` payload that names a trusted `/reclarify` created strictly after the issue's `closed_at`. clarify's `Decide clarify route` step applies the same check to its own event comment. Open issues are routed and authorized exactly as before.

| The numbers that matter | Value |
| --- | --- |
| New optional payload key | `reclarify_comment_id` (8 of GitHub's 10 `client_payload` properties) |
| New handoff env var | `CLAUDE_ISSUE_RECLARIFY_COMMENT_ID`, default empty |
| New `final-merge-resume` / `build-dispatch` flag | `--reclarify-comment-id` |
| New ineligible reasons in clarify's notice | `no_reclarify_comment`, `reclarify_not_trusted`, `reclarify_before_close` |
| Intake refusal reason | `issue_closed`, unchanged |

What this means for operators: to resume a project whose final merge closed its issue, comment `/reclarify` after the merge, as before. A `/reclarify` posted in the same second as the merge is refused, so comment again. The intake's manual `workflow_dispatch` can no longer resume a closed issue.

### For contributors

`final_merge_resume(issue, comments, reclarify_comment_id)` reads the bound comment from the live comment list and fails closed when `closed_at` or its `created_at` is missing. `authorize_target` refuses a closed candidate before asking for comments unless the payload is `trigger: reclarify` with a positive `reclarify_comment_id`, so it makes no more API reads than before (§15). The fire text and queue item format are unchanged.
