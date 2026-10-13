<!-- agents: section="Workflow architecture" -->
CI-triage PR-branch routing (issue #7042): with
`CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED=true` (default `false`),
`scripts/resolve_integration_ref.sh` routes a verified `ai:check-triage` issue
to its failing PR's head branch for clarify, plan, implement, clarify-respond
and validate. It verifies the label, the canonical marker header, an
OWNER/MEMBER/COLLABORATOR author, the body's PR line, the live PR (open, head
and base in this repository, matching head ref) and the branch. Any failure,
including an unreadable or unparseable issue while the flag is on, exits 3,
and the phase fails closed instead of using the default branch. Other resolver
exits keep their existing handling (75 and 2 fail closed in implement and
validate). An explicit `Integration branch:` line still wins.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `CI_TRIAGE_PR_BRANCH_ROUTING` (`scripts/resolve_integration_ref.sh`: `issue= pr= outcome=routed branch=` on stderr, `::error::... outcome=refused reason=label_missing|marker_invalid|not_an_issue|author_untrusted|pr_association_mismatch|pr_lookup_failed|pr_not_open|pr_cross_repository|head_ref_invalid|branch_missing|branch_lookup_failed|issue_read_failed|issue_parse_failed|issue_json_invalid`, exit 3)
LOG_PREFIX.name=CI_TRIAGE_PR_BRANCH_ROUTING
