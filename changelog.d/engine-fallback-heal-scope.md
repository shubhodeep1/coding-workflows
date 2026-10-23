<!-- changelog: fixed -->
- **Refused AI engine fallbacks now open heal issues.** Before this, the heal intake dropped every `engine_fallback` report as `scope_unavailable_no_runs`, after running the diagnosis model, because the report named no run to scope the issue to. A refusal in clarify, plan, orchestrate, security audit, validate or the poller therefore reached nobody except a Telegram WARNING.

`scripts/ai_engine_fallback_report.sh` now puts the reporting run and its attempt in the report (`reporter_run`). Before filing a new heal issue, `scripts/workflow_failure_heal_intake.sh` reads that run once and accepts it only when it belongs to the source repository, has the reported workflow name and at least the claimed attempt, and is still running or finished within `WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS`. The issue is then scoped to that run plus the engine support files (`scripts/ai_engine.sh`, `scripts/claude_engine.py`, `scripts/codex_isolated_exec.sh`, `scripts/clarify_isolated_run.sh`, `scripts/review_untrusted_sandbox.sh`, `scripts/ai_engine_fallback_report.sh`, and the `install-claude` and `claude-pool-token` actions) and filed in coding-workflows. Reports keep one heal issue per role and reason. A repeat from any repository or run is recorded as an occurrence on the open issue, without the run read.

| The numbers that matter | Value |
| --- | --- |
| `engine_fallback` reports skipped as `scope_unavailable_no_runs`, 2026-10-21 07:51–08:13 UTC | 5 of 5 (`SECURITY_AUDIT`, `isolation_unavailable`, from fun-token-multi-chain and tele-funtoken-msg-scoring) |
| Extra GitHub API calls per report | 1 run read, only when a new heal issue would be filed |
| Fixed scope paths | 8, each kept only if it exists at the release commit |

What this means for operators: a refused Claude fallback in a consumer now becomes one `ai:workflow-heal` issue per role and reason. Consumers send the run only after their next `@stable` sync. Until then their reports are skipped quietly as before.

### For contributors

`run_refs` stays empty for this kind, so claim binding, run provenance and log reading do not change. `validate-payload` drops a malformed `reporter_run` without failing the report. A `consumer-app-defect` verdict is filed as `workflow-defect` (`classification_remapped ... reason=engine_fallback_harness_scope`), because the scope names only harness files. A verified run whose scope still does not resolve gets the CRITICAL refusal that every report naming a run gets. New subcommand: `workflow_failure_heal.py verify-engine-reporter-run`. New option: `build-engine-fallback-payload --run-attempt`.
