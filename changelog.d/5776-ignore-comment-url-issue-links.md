<!-- changelog: fixed -->
- **A link to an issue comment no longer makes that issue a PR's linked issue.** The shared body/title fallback ignores issue URLs that carry a `#` fragment, so a `/implement-plan-claude` PR that cites a comment on its source issue no longer gets that issue labelled `ai:merged` or its lineage finalized.

On 2026-09-30, conformance-fix PR #5649 merged into #4867's own project branch. Its body said `Refs #4867` and linked to two comments on #4867. `issue_pr_status.yml` read the comment links as linked issues, labelled #4867 `ai:merged`, and finalized its AI-memory lineage as `merged`. The poller's close sweep then sent a Telegram WARNING on every poll until the label was removed by hand. `extract_repo_scoped_issue_refs_from_text` in `scripts/gh_helpers.sh` now skips a repo-scoped issue URL or path that carries a `#` fragment, whether it follows the issue number directly or a `/…` or `?…` tail, as in a notification link (`…/issues/<n>?notification_referrer_id=…#issuecomment-…`). It reads each URL whole, the way GitHub renders it, so an issue path inside another URL's query or fragment, or inside a URL on another host (`https://example.com/?next=<repo>/issues/<n>`), is never a link of its own, and a backslash-escaped `\)` in a Markdown link destination does not end it early. Closing keywords (`Fixes #N` and the rest) and bare `/issues/<n>` URLs link as before. The same helper feeds the review pipeline's linked-issue fallback, so the review identical-failure cap no longer labels a comment-linked issue `ai:review-blocked`.

| The numbers that matter | Value |
| --- | --- |
| URLs no longer counted as linked issues | `https://github.com/<repo>/issues/<n>#…`, `<repo>/issues/<n>#…`, and the same with a `/…` or `?…` tail before the `#`, including when that tail holds another `<repo>/issues/<m>` |
| Still counted | `close`/`fix`/`resolve` keywords with `#<n>`, and `/issues/<n>` URLs and paths with no fragment, query strings included, each Markdown link on its own when links sit side by side |
| Callers of the helper | `issue_pr_status.yml`, `review_autofix.yml` (5 sites), `scripts/review_collect_pr_metadata.sh`, `scripts/review_rb_judge.sh` |
| New GitHub API calls | 0 |

What this means for operators: citing a comment in a PR body is safe again. Only a closing keyword or a bare issue URL ties a PR to an issue. Consumer repos pick up the change from the `scripts/gh_helpers.sh` the workflows fetch on the next `@stable` sync, with no variable to set.

### For contributors

`tests/test_issue_pr_status_comment_url_links.py`, run in `ci.yml`, pins the helper and drives the status-sync steps. Its step tests cover four cases: the #5649 shape and a `Fixes #N` PR into a `claude/implement-plan-*` branch leave the issue alone, while an orchestrator-managed child on its integration branch and a default-branch `Fixes #N` merge still label and close. The label/lineage rule itself is the #4813 target-branch gate. It loads the stub harness of `tests/test_issue_pr_status_target_branch_gate.py` by path.
