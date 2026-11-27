<!-- agents: section="Workflow file size limit" -->
- Verified `.codex-workflow-src` fallback (#10221): every `codex-agent` step
  wrapper in `review_autofix.yml` falls back from
  `${SUPPORT_SCRIPTS_DIR:-}/<script>` to
  `${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/<script>` only when
  `WORKFLOW_SUPPORT_SOURCE_VERIFIED=true` and
  `git -C "${GITHUB_WORKSPACE}/.codex-workflow-src" rev-parse HEAD` equals
  `${SCRIPT_REF:-unset}`. Only "Ensure workflow support source checkout" sets
  the flag (through `$GITHUB_ENV`), after its HEAD-vs-`SCRIPT_REF` check.
  Before that step, the workspace path may hold files committed in the PR
  ("Checkout repo" puts the PR head at the workspace root). An early failure,
  such as the `.opencode` reject step, would otherwise let `failure()`,
  `always()` and `!cancelled()` steps source those files with `GH_PAT` set.
  New wrappers must use the gated second line exactly. `test_moved_steps_run_only_the_resolving_wrapper`
  and `test_workspace_fallback_requires_verified_support_checkout` in
  `tests/test_review_autofix_step_scripts_contract.py` pin it, and
  `test_workspace_fallback_never_sources_unverified_pr_tree_copy` runs the
  real wrapper text. There is no `.codex-workflow-src-main/scripts`
  fallback in these wrappers.
