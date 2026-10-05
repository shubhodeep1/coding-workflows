<!-- changelog: security -->
- **The unblock judge no longer opens issues from untrusted pull requests.** Issue-creating verdicts on fork PRs or PRs without a verified trusted author instead explain the block and close the PR; a maintainer can reopen it or file an issue manually.

For maintainers: issues derived from trusted same-repository PRs now carry an audit-only provenance marker with the source PR, author, head repository and head SHA. No new API calls or issue-open gate changes are required.
