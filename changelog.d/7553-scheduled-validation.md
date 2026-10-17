<!-- changelog: added -->
- **A consumer can now validate its default branch every week without an orchestrator project.** Set the repository variable `AI_VALIDATE_SCHEDULE_ENABLED` to `true`, and the `ai-validate.yml` wrapper runs every Monday at 04:17 UTC.

Before this, a consumer's `.ai/validate.yml` ran only at project completion, for a merged pull request whose issue carried `ai:orchestrator-validate-required`, or by hand, so a repository with no active project could go weeks without validation (tele-funtoken-msg-scoring#4606).

- The wrapper (`workflow-templates/ai-validate.yml`) gains a fixed `schedule:` trigger. Its job runs on that trigger only when `AI_VALIDATE_SCHEDULE_ENABLED` is `true`; otherwise the job is skipped and no runner starts. The cron is fixed because `on.schedule` cannot read variables.
- A scheduled run validates the default branch with tracking issue `0` and no target ref. `validate.yml` detects it from the caller event (`VALIDATE_SCHEDULED_MODE`); there is no new workflow input.
- A scheduled run calls no model. `scripts/validate_process.sh` honours the new switch `VALIDATE_DETERMINISTIC_ONLY` (default off): it skips discover, diagnose and self-heal, renders and runs only the committed `.ai/validate.yml`, and reports `raw_status=harness_missing` when that file is missing or `raw_status=tests_failed` when tests fail. Per-phase Telegram alerts are silenced in that run, and diagnose never files fix-up issues.
- `scripts/validate_scheduled_sink.sh` reports the result in one issue labelled `ai:scheduled-validation-failed`, written by the `GH_PAT` account and starting with `<!-- ai:scheduled-validation:v1 -->`. It opens with one Telegram WARNING, is edited in place while the branch stays red, and closes when a later scheduled run passes. A failed identity or issue-list read writes nothing. Clarify ignores the label, so the issue never enters the pipeline.

Manual (`/revalidate`, dispatch) and orchestrator validation runs are unchanged. A sink for red `validation-refresh.yml` results is left to a follow-up.
