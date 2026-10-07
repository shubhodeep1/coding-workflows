<!-- changelog: added -->
- **Claude engine fallbacks to codex now alert and open a heal issue.** A clarify, plan, implement or clarify-respond run that could not start Claude sends one Telegram WARNING. A setup or code fault also gets an `ai:workflow-heal` issue to fix its root cause.

A fallback keeps the run going on codex, so the run succeeds and nothing looked wrong. The fallback's own Telegram note was sent from the model steps, which hold no Telegram secret, so it never arrived. From 2026-10-04 to 2026-10-06 all 129 implement jobs that selected Claude ran on codex (`cli_missing`) without an alert. Each of `clarify.yml`, `plan.yml`, `implement.yml` and `orchestrate_clarify_respond.yml` now ends its phase job with "Collect AI engine fallbacks" and runs a new `engine-fallback-report` job (`scripts/ai_engine_fallback_report.sh`). The job alerts once per run and sends each defect to the workflow failure heal intake as an `engine_fallback` report. The intake verifies the claimed `AI_ENGINE_FALLBACK` line in the run's own job log, then keeps one open heal issue per role and reason across workflows and repositories. Later fallbacks for the same cause become occurrence comments on it.

| The numbers that matter | Value |
| --- | --- |
| Alerts per run | 1 Telegram WARNING, every fallback listed |
| Heal issue key | role + `AI_ENGINE_FALLBACK` reason + pool reason |
| Capacity fallbacks (`all_gated`, every account at `usage_limit`) | alert only, no issue |
| API calls per reported run | 1 dispatch per distinct defect |
| Workflows wired | 4 (clarify, plan, implement, clarify-respond) |

What this means for operators: a Claude role silently running on codex now pages you once per run, and its cause lands in the pipeline as a heal issue titled `Workflow heal: Claude engine fell back to codex for <ROLE> (<reason>[/<pool reason>])`. `WORKFLOW_HEAL_ENABLED=false` keeps the alert and drops the issue. Consumers get the jobs with the next `@stable` promotion.

### For contributors

- `ai_engine_fallback <role> <reason> [class]` appends `role= reason= class=` to `AI_ENGINE_FALLBACK_RECORD_FILE` (default `$RUNNER_TEMP/ai-engine-fallbacks.txt`), and skips its own Telegram note when `AI_ENGINE_FALLBACK_REPORTER=true`. `claude_run` and the clarify sandbox record `class=capacity` for `all_accounts_failed` only when every account returned `usage_limit`.
- `claude_engine.py fallbacks --record-file F --pool-reason R` classifies the record. The "Resolve Claude credential" steps gained `id: claude_pool` so the collector can read their `reason` output.
- New heal helpers: `build-engine-fallback-payloads`, `engine-fallback-log` and `verify-engine-fallback-provenance`. Log evidence needs a timestamped line the step printed itself, so echoed step scripts and untimestamped env continuation lines are ignored.
- The intake's diagnosis prompt filled empty TSV fields before `read`. A job with no failed step used to shift its log path into `failing step` and show `(log unavailable)` to the model.
