<!-- agents: section="Workflow architecture" -->
CI-triage PR-branch routing (issue #7042): with
`CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` on (the default; `false` turns it off),
`scripts/resolve_integration_ref.sh` routes a verified `ai:check-triage` issue
to its failing PR's head branch for clarify, plan, implement, clarify-respond
and validate. It verifies the label, the canonical marker header, an
OWNER/MEMBER/COLLABORATOR author, the body's PR line, the live PR (open, head
and base in this repository, matching head ref) and the branch. Any failure,
including an unreadable or unparseable issue while the flag is on, exits 3,
and the phase fails closed instead of using the default branch. Other resolver
exits keep their existing handling (75 and 2 fail closed in implement and
validate). An explicit `Integration branch:` line still wins. An issue with
no `ai:check-triage` label whose only triage marker is a
`check-failure-triage:pr=<n>` line (the unblock judge's CI-failure reissue,
#7032) is not routed and keeps the default branch.
Clarify alone keeps the default branch on a `reason=pr_not_open` refusal
(the PR merged or closed), so the check-triage resolution gate (#7095) can
close the issue with evidence; the refusal is logged as a `::notice::`. While
routing is on, clarify, plan and clarify-respond also fail closed on a
rate-limited resolver (exit 75, `INTEGRATION_REF_RESOLVE outcome=rate_limited`)
instead of using the default branch.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `CI_TRIAGE_PR_BRANCH_ROUTING` (`scripts/resolve_integration_ref.sh`: `issue= pr= outcome=routed branch=` on stderr, `::error::... outcome=refused reason=label_missing|marker_invalid|not_an_issue|author_untrusted|pr_association_mismatch|pr_lookup_failed|pr_not_open|pr_cross_repository|head_ref_invalid|branch_missing|branch_lookup_failed|issue_read_failed|issue_parse_failed|issue_json_invalid`, exit 3)
LOG_PREFIX.name=CI_TRIAGE_PR_BRANCH_ROUTING
