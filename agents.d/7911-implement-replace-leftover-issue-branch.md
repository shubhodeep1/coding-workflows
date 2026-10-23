<!-- agents: section="Stable log prefixes (contractual)" -->
- `IMPLEMENT_STALE_BRANCH` (`scripts/implement_stale_branch_check.sh`, called by `implement.yml` "Push branch": `branch=<ai/issue-N> outcome=replace reason=no_open_pr`, or `outcome=keep reason=not_issue_branch|invalid_repo|open_pr count=1|api_unavailable rc=<n>|malformed_response`)
LOG_PREFIX.name=IMPLEMENT_STALE_BRANCH
