<!-- changelog: fixed -->
- **Attempt logs and the implement failure comment now name the engine that ran.** A Claude run no longer appears as "Codex" in the clarify, clarify-respond, plan, implement and orchestrate logs.

Claude is the default engine for these roles, but every retry loop still printed "Codex …" for each attempt. On shubhodeep1/tele-funtoken-msg-scoring#4687 the clarify log read `Codex clarify attempt 1/3 (engine claude)...` for a run that used Claude throughout. Each loop now prints the engine actually in use, for example `Claude clarify attempt 1/3...` and `Claude planning succeeded on attempt 1 (…)`. When Claude is unavailable and the run falls back to Codex part-way through an attempt, the remaining lines say "Codex". The implement failure comment posted on the source issue follows the same rule: its heading reads `## Claude implement diagnostics` or `## Codex implement diagnostics`, and its reason reads `Claude implement failed after 5 attempts`, `Claude bailed …` and so on.

| The numbers that matter | Value |
| --- | --- |
| Retry loops renamed | 5 (`clarify.yml`, `orchestrate_clarify_respond.yml`, `scripts/run_plan_codex.sh`, `implement.yml`, `orchestrate.yml`) |
| Step names changed | 0 (`Run Codex implementation`, `Run Codex planning` and the rest keep their names) |

What this means for operators: read the engine from the attempt line instead of the `AI_ENGINE_SELECTED` line. Anything that searched logs or issue comments for the literal text `Codex implement failed`, `Codex bailed` or `Codex planning attempt` should match `(Claude|Codex)` instead. No workflow or script in this repository matched on that text. Workflow failure heal fingerprints are unchanged: `normalize_error_line()` in `scripts/workflow_failure_heal.py` maps a leading `Claude` in an error line (and `last_status=claude`) back to `codex`, so a failure keeps one fingerprint whichever engine ran.

### For contributors

`implement.yml` gets the name from `implement_engine_display_name()`. That function reads `AI_ENGINE_RESOLVED_IMPLEMENT` and the `claude-implement.unavailable` marker that `scripts/codex_thread_reuse.sh` writes on fallback. It is refreshed at the start of each attempt and after the editor call. On the heal route, a fallback caused by missing engine support files writes no marker, so it is still logged as Claude. In `orchestrate.yml`, the `last_status` value in the final error line now starts with the lowercase engine (`claude exit 1`, `codex timeout 124`).
