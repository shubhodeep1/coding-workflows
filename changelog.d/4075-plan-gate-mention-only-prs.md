<!-- changelog: fixed -->
- **Planning is no longer skipped by a PR that merely mentions the issue, and `Target branch:` now routes an issue to its integration branch.** Two pipeline defects that together stalled issue #4073 and blocked its re-issue #4075.

`plan.yml` ("Skip when issue already has a PR") and `implement.yml` ("Safety check for existing PR") counted every open pull request that cross-referenced the issue as the issue's own PR. PR #4072, a `main` fix that cited #4073 as context, kept #4073's three planning runs from doing anything, with no comment and no label change, until stall recovery closed it 76 minutes later and re-issued it as #4075. Both gates now skip only for the issue's own implementation PR: an open PR on the conventional `ai/issue-<N>` head, or an open PR whose body closes the issue with a supported short, qualified, or GitHub issue URL reference (the same boundaries `_pr_json_closes_issue` uses in `scripts/orchestrate_poll_process.sh`). Mention-only PRs are logged as ignored. The re-issue #4075 then planned on `main` because its body names the branch as ``**Target branch:** `orchestrator/project-3965` ``, which `scripts/resolve_integration_ref.sh` did not recognise, and the planner correctly emitted `BLOCKED: integration branch mismatch`. `Target branch:` is now an accepted alias of `Integration branch:` in child and tracking issue bodies, in both the shell resolver and `scripts/orchestrate_lib.py`; the canonical line wins when both are present.

| The numbers that matter | Value |
| --- | --- |
| GitHub API calls per gate | 2 (timeline + paginated open-PR inventory), was 1 |
| Planning runs silently skipped on #4073 | 3 (runs 34426969864, 34431425213, 34435890933) |
| New resolver fixtures | 4 under `tests/fixtures/integration_ref_resolver/` |

What this means for operators: an issue that an unrelated open PR references with `Refs #N` now plans and implements normally, and a human-written issue can steer the pipeline to an integration branch with a `Target branch:` line instead of the orchestrator's metadata footer. A `Target branch:` that names a branch that does not exist fails safe the same way a missing `Integration branch:` does instead of falling back to the default branch.

### For contributors

`tests/test_stall_recovery_pr_lookup.py` now exercises the plan.yml gate as well as the implement.yml one, with a `MOCK_GH_PULLS_JSON` hook in the shared `gh` stub for the paginated open-PR inventory. The alias pattern lives in two places by design (shell and Python parity is pinned by `test_resolve_integration_ref_parity_for_fixtures`). Workflow shell steps receive the resolved ref through step-local environment variables, so valid Git ref characters cannot be reinterpreted as shell syntax when the ref is logged or stored as the implementation PR base.
