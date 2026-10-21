<!-- changelog: changed -->
- **The planner can now read the default branch as `isolated-base`.** On orchestrator items the planner checks out the integration branch, so it could not see the production code a plan has to restore or keep working.

`scripts/run_plan_codex.sh` fetches the default branch's tip (one depth-1 `git fetch`, no GitHub API call) into `refs/codex-isolated/base` and exports `CODEX_ISOLATED_GIT_BASE_REF`, the same mechanism implement uses since #7548. The sandbox gets the branch filtered like the checkout, and the planner prompt says how to read it. A failed fetch only leaves the branch out. Set the repository variable `PLAN_ISOLATED_BASE_ENABLED=false` to turn it off.
