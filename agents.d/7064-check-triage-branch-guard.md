<!-- agents: section="Workflow architecture" -->
Check-triage target-branch guard (issue #7064): with
`CHECK_TRIAGE_BRANCH_GUARD_ENABLED=true` (default `false`), `plan.yml`'s
`Check-triage target-branch guard` step blocks planning of an `ai:check-triage`
issue through the existing `BLOCKED:` handler instead of planning it on the
default branch. It reads only the labels and `steps.refctx.outputs.resolver_status`
(`resolved`, `routed`, `none`, `failed`, `unavailable`). `resolved` (an
explicit `Target branch:` / `Integration branch:` line, or the tracking issue's
line) passes with `reason=metadata_present`; `routed` (a verified PR head from
`CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED`, matched from the resolver's own
`CI_TRIAGE_PR_BRANCH_ROUTING ... outcome=routed` line) passes with
`reason=pr_head_routed`; anything else, or unreadable labels, blocks. It never
infers a branch from issue prose.

<!-- agents: section="Stable log prefixes (contractual)" -->
- `CHECK_TRIAGE_BRANCH_GUARD` (`plan.yml` "Check-triage target-branch guard": `issue= enabled= check_triage=true|false|unknown resolver_status=resolved|routed|none|failed|unavailable|unknown outcome=skip|pass|blocked reason=disabled|not_check_triage|metadata_present|pr_head_routed|no_target_branch_metadata|target_branch_unverified|labels_unavailable`)
LOG_PREFIX.name=CHECK_TRIAGE_BRANCH_GUARD
