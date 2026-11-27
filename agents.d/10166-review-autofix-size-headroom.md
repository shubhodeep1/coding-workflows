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
  moved five more `codex-agent` step bodies out:
  `scripts/review_autofix_step_telegram_rb_judge_decision.sh`,
  `scripts/review_autofix_step_push_pending_commits.sh`,
  `scripts/review_autofix_step_static_context.sh`,
  `scripts/review_autofix_step_resolve_ai_engine.sh` and
  `scripts/review_autofix_step_capture_runtime_context.sh` (the latter runs
  under `always()`, so its wrapper skips with a warning; the "Mark linked issues
  review-blocked (autofix exhaustion)" body stays in #9654's
  `scripts/review_autofix_step_mark_review_blocked_exhaustion.sh`, and the
  "Telegram editor-noop-suspicious warning" body stays in #9944's
  `scripts/review_autofix_step_editor_noop_suspicious_warning.sh`; one
  script and one registry entry per step). The judge-decision script opens
  with `set -euo pipefail` then `set +u +o pipefail` to keep the runner's
  default `bash -e` options of the inline body.
