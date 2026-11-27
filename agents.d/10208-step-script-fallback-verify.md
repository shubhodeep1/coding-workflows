<!-- agents: section="Workflow file size limit" -->
- Verified `.codex-workflow-src` fallback (#10208): every `codex-agent` step
  wrapper in `review_autofix.yml` (and the "Classify infra-failed review
  round" / "Write review findings record" helper lookups) carries one
  verify line right after its `.codex-workflow-src/scripts` fallback line:
  when the resolved path is under `${GITHUB_WORKSPACE}/.codex-workflow-src/`,
  it is kept only if `SCRIPT_REF` is a 40-hex SHA, the
  `REVIEW_SUPPORT_SOURCE_VERIFIED_SHA` sentinel equals it, and
  `env -u GIT_DIR -u GIT_WORK_TREE git -C .codex-workflow-src rev-parse HEAD`
  equals it (the `env -u` matters: "Activate workspace shell context" exports
  `GIT_DIR`/`GIT_WORK_TREE` for the split PR workspace, and without it git
  reads the PR checkout's HEAD and refuses every fallback). Otherwise the path is
  emptied and the wrapper's missing-script branch runs (warning + `exit 0` for
  `always()` steps, error + `exit 1` for the rest). Only "Ensure workflow
  support source checkout" writes the sentinel, after its SHA check, so a job
  that fails before that step never sources a PR-committed
  `.codex-workflow-src/scripts/*`. The PR-head "Checkout repo" step sets
  `persist-credentials: false`. New wrappers must copy the verify line;
  `tests/test_review_autofix_step_scripts_contract.py` pins it and
  `tests/test_review_autofix_step_fallback_verify.py` runs each wrapper
  against a planted script.
