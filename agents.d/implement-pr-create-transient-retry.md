<!-- agents: section="Stable log prefixes (contractual)" -->
- `IMPLEMENT_PR_CREATE` (`implement.yml` "Create Pull Request", after a failed `gh pr create` that found no existing PR: `issue=<N> head=<ai/issue-N> attempt=<n>/3 outcome=retry reason=transient wait_secs=<s>` or `outcome=give_up reason=no_existing_pr`)
LOG_PREFIX.name=IMPLEMENT_PR_CREATE
