<!-- agents: section="Orchestrator tracking-issue comment markers" -->
Before filing each deferred advisory, `security_pass_file_deferred_advisory_followups`
runs `security_pass_advisory_cited_code_status`. It fetches the default and
integration branches and, for a numeric final PR, `refs/pull/<n>/head` once (local
git, no API calls; a failed default-branch fetch makes every row `unknown`), takes the cited line plus up to
two lines either side at `audited_head_sha`, and looks for that whitespace-trimmed
block as one contiguous run in `origin/<default>` (`present` / `absent` /
`unknown`). On `absent` the body says the cited lines are not on the default
branch, tells the pipeline not to recreate removed code, and asks it to check
whether the vulnerable behaviour still exists, fix it if so, and add a regression
test either way. `present` and `unknown` keep the "already on the default branch"
line. The issue is filed in every case. shubhodeep1/fun-token-multi-chain#710 is the
incident: #667 reverted the cited coinflip code between the audit and the final
merge, and planning blocked with "nothing to implement".

<!-- agents: section="Stable log prefixes (contractual)" -->
- `SECURITY_PASS_ADVISORY_CITED_CODE` (`scripts/orchestrate_poll_process.sh`: `tracking_issue=` `finding=` `default_branch=` `status=present|absent|unknown`)
LOG_PREFIX.name=SECURITY_PASS_ADVISORY_CITED_CODE
