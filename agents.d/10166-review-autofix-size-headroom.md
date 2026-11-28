<!-- agents: section="Workflow file size limit" -->
- Test ceiling and early warning (#10166): `WORKFLOW_TEST_CEILING_BYTES`
  (430,000) in `scripts/ci_budget_watch.py` is the one size ceiling the
  contract tests use (`tests/test_review_autofix_review_pipeline_contract.py`,
  and `MAX_WORKFLOW_BYTES` in `tests/test_implement_step_scripts_contract.py`
  and `tests/test_test_and_mark_stable_step_scripts_contract.py` import it).
  The `CI_BUDGET_WORKFLOW_WARN_BYTES` default is 400,000 and must stay at
  least 20,000 bytes under that ceiling
  (`tests/test_ci_budget_watch.py::test_default_warn_threshold_is_under_the_test_ceiling`),
  so the watcher opens its `ai:ci-budget` issue before a test fails.
  Workflow-size findings skip the re-file cooldown. `review_autofix.yml`
  moved four more `codex-agent` step bodies out:
  `scripts/review_autofix_step_telegram_rb_judge_decision.sh`,
  `scripts/review_autofix_step_push_pending_commits.sh`,
  `scripts/review_autofix_step_static_context.sh` and
  `scripts/review_autofix_step_resolve_ai_engine.sh` (the "Mark linked issues
  review-blocked (autofix exhaustion)" body stays in #9654's
  `scripts/review_autofix_step_mark_review_blocked_exhaustion.sh`, and the
  "Telegram editor-noop-suspicious warning" body stays in #9944's
  `scripts/review_autofix_step_editor_noop_suspicious_warning.sh`; one
  script and one registry entry per step). The judge-decision script opens
  with `set -euo pipefail` then `set +u +o pipefail` to keep the runner's
  default `bash -e` options of the inline body. Two bodies outside
  `codex-agent` also moved: `scripts/review_autofix_step_post_merge_validate_dispatch.sh`
  ("Dispatch standalone validate for orchestrator short-circuit issues") and
  `scripts/review_autofix_step_fingerprint_cap_block.sh` ("Apply
  identical-failure cap outcome"). Their jobs have no staged support bundle
  but check out and verify their own `.codex-workflow-src` ("Verify workflow
  support source" exports `WORKFLOW_SUPPORT_REF`), so the wrapper reads only
  `${GITHUB_WORKSPACE}/.codex-workflow-src/scripts/<script>`, fails closed
  unless that checkout's HEAD equals `WORKFLOW_SUPPORT_REF`, and has no
  `SUPPORT_SCRIPTS_DIR` path. They are listed in
  `REVIEW_AUTOFIX_SUPPORT_CHECKOUT_JOB_STEP_SCRIPTS` (job id per script) in
  `tests/review_autofix_step_scripts.py`; a sparse support checkout must
  list the script. "Capture runtime context"
  stays inline: it runs under `always()`, possibly before the trusted support
  checkout, and the wrapper's `.codex-workflow-src` fallback would then source
  a file from the PR tree (#10166 security pass). Keep `always()` steps that
  can run before support staging inline.
