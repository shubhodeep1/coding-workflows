<!-- agents: section="Interactive slash-command model selection" -->
CI also runs on pull requests into, and pushes to, `orchestrator/project-*`
integration branches. The template-only live-copy preparation runs for those
PRs (`--before` = PR base SHA) and pushes (`--before` = the push's `before`).
When that value is empty, all zeros (branch creation) or unreachable (force
push), the step logs a `::warning::` and uses the merge-base of `HEAD` with
`origin/main`, because `sync_claude_live_copies.py` skips drift recovery
without a reachable `--before`. Integration-branch pushes are not healed by
the workflow failure heal intake, and `budget-watch` runs on `main` pushes only.
