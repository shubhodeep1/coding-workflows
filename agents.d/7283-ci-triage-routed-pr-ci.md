<!-- agents: section="Workflow architecture" -->
Routed fix PRs get CI, and resolver staging fails closed (issue #7283):
`ci.yml` runs pull-request CI for every base (`pull_request: branches:
["**"]`; push CI stays on `main`, `stable` and `orchestrator/project-*`). A
fix PR routed to a failing PR's head branch therefore gets its own
`CI / lint`, and `_pr_auto_merge_required_ci_check_name` in
`scripts/pr_checks_lib.sh` defaults to `lint` for every base in this
repository (consumers stay `none`). The "Resolve integration ref" step of
`plan.yml`, `clarify.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`
and `validate.yml` fails with `::error::INTEGRATION_REF_RESOLVE
outcome=resolver_unavailable reason=stage_failed|pin_failed|script_missing
ci_triage_routing=on` and writes no `ref` when the resolver cannot be staged,
pinned (the unpinned main-fallback clone counts as `pin_failed`) or found
while `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` is on (`true`, `1`, `yes` or
`on`, any case, as the resolver parses it). With the flag off, the
default-branch fallback is unchanged.
The `tests-hooks-and-orchestrator` job's "Prepare template-only PR live copies
for parity tests" step now prepares every pull request except one into
`stable` (`github.base_ref != 'stable'`), so a routed fix PR into an
`ai/issue-*` head gets the same dry-run preparation as a PR into `main`.
`scripts/check_failure_triage.sh`'s base gate skips a failed CI workflow run
whose base is `ai/issue-<n>` and has no completed push run
(`CHECK_TRIAGE skip reason=routed_head_base`), because push CI never runs on
that branch and the routed fix PR's own review/autofix loop owns the failure.
