<!-- changelog: added -->
- **Planning can now refuse an `ai:check-triage` issue that does not name its target branch, instead of planning it on the default branch.** Off by default; turn it on with the repository variable `CHECK_TRIAGE_BRANCH_GUARD_ENABLED=true`.

Issue #6921 stopped in planning because its body had no standalone target-branch line, and without one the integration resolver returns nothing, so planning checks out the default branch. With the switch on, `plan.yml` blocks such an issue before the model runs. It uses the existing blocked path: the issue gets `ai:blocked`, a comment explains what to add, and a CRITICAL alert is sent. The guard reads only the resolver's outcome (the new `resolver_status` output of `Resolve integration ref`: `resolved`, `routed`, `none`, `failed` or `unavailable`) and the issue's labels. It never guesses a branch from the issue text, and a declared branch that does not exist also blocks. A branch from verified PR-head routing (`CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED`) passes, logged as `reason=pr_head_routed`.

What this means for operators:
- Add a standalone `Target branch: <branch>` or `Integration branch: <branch>` line to the blocked issue's body, then reply `/answer`.
- A triage issue carries a branch line only when `CHECK_TRIAGE_PR_BRANCH_ROUTING_ENABLED`, `CHECK_TRIAGE_PR_HEAD_ROUTING_ENABLED` or `TRIAGE_PR_HEAD_BRANCH_METADATA_ENABLED` adds one. With none of them on, turning the switch on blocks every new triage issue until its body names a branch.
- Log line: `CHECK_TRIAGE_BRANCH_GUARD issue=<n> enabled=<bool> check_triage=<bool> resolver_status=<token> outcome=skip|pass|blocked reason=<token>` (prefix registered in the agents.md stable log prefixes). No new GitHub API calls.
