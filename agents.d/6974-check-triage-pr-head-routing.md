<!-- agents: section="Workflow architecture" -->
Check-failure triage can route its fix to the failing PR's own head branch:
`CHECK_TRIAGE_PR_HEAD_ROUTING_ENABLED` (default `false`) adds one verified
`- **Integration branch:** `<head ref>`` line to the triage issue. The collect
step checks open state, that both `head.repo` and `base.repo` name this
repository, a matching nonempty check/PR head SHA and a valid ref; a verified
route bypasses the base-branch gate (`base_gate outcome=file
reason=pr_head_route`), because the fix lands on the PR's branch. An unverified
route logs `CHECK_TRIAGE skip reason=routing_unverified detail=<reason>`, sends
a WARNING and files nothing. The diagnose step re-checks the hand-off and, on a
mismatch, logs `error reason=routing_unverified`, sends a WARNING and fails
closed. The body validator admits only that one script-generated line.
