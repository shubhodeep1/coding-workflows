<!-- changelog: fixed -->
- **A fix PR routed to a failing PR's branch now waits for its own CI before it merges, and a resolver that cannot be staged no longer sends routed work to the default branch.** Activation verification of PR #7050 found both gaps.

Pull-request CI ran only for PRs into `main`, `stable` and `orchestrator/project-*`, and the auto-merge wait required `lint` only for those bases. A fix PR into a failing PR's `ai/issue-<n>` head could therefore merge with no CI run of its own. `ci.yml` now runs pull-request CI for every base, and `scripts/pr_checks_lib.sh` requires `lint` for every base in this repository. Those PRs also get the template live-copy preparation before the parity tests, and check-failure triage does not file an issue for a failing PR into an `ai/issue-<n>` branch, which has no push CI to compare with (`CHECK_TRIAGE skip reason=routed_head_base`).

The "Resolve integration ref" step in `plan.yml`, `clarify.yml`, `implement.yml`, `orchestrate_clarify_respond.yml` and `validate.yml` fell back to the default branch when it could not stage, pin or find the resolver, even with CI-triage routing on. With `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` on, the step now fails instead, and the unpinned main fallback clone counts as a pinning failure.

| The numbers that matter | Value |
| --- | --- |
| Pull-request CI bases | every branch (push CI unchanged) |
| Log line | `INTEGRATION_REF_RESOLVE outcome=resolver_unavailable reason=stage_failed\|pin_failed\|script_missing` |
| Routing flag default | `false` (fallback unchanged) |

What this means for operators: with routing on, a GitHub clone failure now fails the phase run instead of planning or implementing on the default branch. Re-run the phase once GitHub recovers. PRs into feature branches now get a CI run.
