<!-- agents: section="Workflow architecture" -->
Superseded CI-repair issues (#7216, follow-up of #7185): `plan.yml`'s "Close
superseded CI-repair issue (merged PR)" step runs after "Skip stale /answer
comments" and before the `/answer` claim. It runs
`scripts/plan_superseded_merged_pr.py classify` from the verified support
checkout only (`python3 -I -B`, never the branch checkout). An issue qualifies
when it carries `ai:check-triage` with a trusted author (pipeline login or
OWNER/MEMBER/COLLABORATOR) and its header (before the first `---`) has a Pull
request line for this repository matching the `check-failure-triage:pr=`
marker, or when the pipeline login wrote it and its line-start re-issue
references ("Re-issued from #N", "Re-issued by the unblock judge from #N",
"- Replaces: #N"; never follow-up references such as "Accepted with this
follow-up") lead, within `ORIGIN_MAX_DEPTH` / `ORIGIN_MAX_ISSUES`, only to
trusted triage issues naming one PR and check. `ai:security` issues are not
walked. A merged, same-repository PR whose named check succeeded on its final
head gets one `<!-- ai:plan-superseded:v1 pr=<N> head=<sha> -->` comment
(deduplicated on the pipeline login's marker), `ai:closed`, phase labels
removed, a "not planned" close and `SKIP_PLAN=true`. Merged with the check
failed or pending: planning continues with a "MERGED PULL REQUEST FACTS"
block, and when the resolved ref is the PR's former head the step checks out
its base branch (`PLAN_REROUTED_REF`, mismatch forced `false`). Every other
case, including read failures, plans as usual. Kill switch
`PLAN_SUPERSEDED_MERGED_PR_CHECK_ENABLED` (default `true`). Implement's own
branch resolution is unchanged.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `PLAN_SUPERSEDED_MERGED_PR` (`plan.yml` "Close superseded CI-repair issue (merged PR)" and `scripts/plan_superseded_merged_pr.py`: `issue= source= pr= head= check= outcome=superseded|rerouted|plan|skip reason=not_candidate|no_source|origin_unverified|untrusted_origin|mixed_lineage|ambiguous_source|identity_unavailable|pr_read_failed|pr_open|pr_closed_unmerged|fork_head|check_unknown|merged_check_not_passed|helper_missing|classifier_failed|comment_failed|reroute_failed|disabled label=applied|failed close=done|failed comment=posted|present`)
LOG_PREFIX.name=PLAN_SUPERSEDED_MERGED_PR
