<!-- changelog: changed -->
- **A CI-failure re-issue whose PR merged into a non-default branch now runs on that branch.** `scripts/resolve_integration_ref.sh` resolves an unblock-judge re-issue to the base branch of its owning PR when that PR merged into a non-default branch of the same repository (for example an orchestrator integration branch). Before this, the re-issue planned and implemented against the default branch, which does not hold the failing code.

The unblock judge (`scripts/unblock_actions.py`) writes a re-issue for a CI failure whose owning PR has been verified with only a `<!-- check-failure-triage:pr=<n> -->` lineage marker: no `ai:check-triage` label and no `Integration branch:` line. The resolver used to skip such bodies. It now routes only the exact shape the unblock judge writes: the marker on line 1, `Re-issued by the unblock judge from #N.` on line 2, a trusted author (`OWNER`, `MEMBER` or `COLLABORATOR`), no `ai:orchestrator-managed` label and no tracking line. It reads the PR once through the rate-limit-aware `gh_api_retry`. If the PR merged, both sides are this repository, its `base.ref` passes the head-ref safety rules and is not `base.repo.default_branch`, and the branch still exists, the resolver prints it and logs the existing `CI_TRIAGE_PR_BRANCH_ROUTING ... outcome=routed` line. A PR that did not merge, or that merged into the default branch, keeps today's default-branch result. A failed PR read, a cross-repository PR, an unsafe base ref (new reason `base_ref_invalid`) or a deleted base branch refuses with exit 3 instead of falling back to the default branch. A rate-limited read exits 75. The behaviour is gated by the existing `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` (default `true`). No workflow file changes.

| The numbers that matter | Value |
| --- | --- |
| New GitHub API calls per eligible re-issue | 2 (one PR read, one branch read); none on any other issue |
| New refusal reasons | 1 (`base_ref_invalid`) |
| New environment variables | 0 |

What this means for operators: a re-issue whose merged PR's base branch was deleted afterwards now stops with a `CI_TRIAGE_PR_BRANCH_ROUTING ... reason=branch_missing` refusal instead of planning on the default branch. Recreate the branch, or add an `Integration branch:` line to the re-issue, to unblock it.

Refs #9169.
