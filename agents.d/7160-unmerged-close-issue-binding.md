<!-- agents: section="Workflow architecture" -->
Unmerged PR close binding (#7160): in `issue_pr_status.yml`'s "Update linked
issue labels when PR closes" step, a PR closed without merging labels
`ai:closed`, closes and finalizes the lineage of an issue only when the PR head
repository (`PR_HEAD_REPO`, from the event payload) is this repository, the
head branch is `ai/issue-<n>` for that issue, and the PR author
(`PR_AUTHOR_LOGIN`) equals the `GH_PAT` login. The login is read with one
`GET /user` at most once per run, and only after an issue passes the first two
checks; a failed or empty read fails closed. Any other referenced issue is left
unchanged and kept out of `LINEAGE_FINALIZE_ISSUE_NUMBERS`, with the line
`Unmerged close of PR #<pr> left issue #<n> unchanged (reason=foreign_head|unbound_issue|identity_unavailable|author_mismatch)`.
Merged-PR rules (#5776, #6631) and the tracking-issue skip are unchanged.
