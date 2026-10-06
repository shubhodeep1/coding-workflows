<!-- changelog: fixed -->
- **Failed clarify, plan and implement runs now open workflow heal issues, and Claude-engine planning no longer crashes on heal issues.** Every failed review/autofix run is reported too, not only the second one in a row.

Until now a failed clarify, plan or implement run reached workflow failure heal only if the issue later picked up an escalation label such as `ai:needs-human`. The stall poller never adds one by default, so issues #6413, #6373 and #6392 failed planning four times each on 2026-10-05 and no heal issue was opened. `clarify.yml`, `plan.yml` and `implement.yml` now carry a `heal-report` job that runs after the phase job fails and dispatches a `phase_failure` report to `workflow-failure-heal-intake.yml`. The intake reads the failed job's log, keeps one open heal issue per source issue, and escalates a heal issue whose own run fails with the failure it was filed for (`ai:workflow-heal-escalated`, CRITICAL Telegram alert). Those three plan failures came from `plan.yml` copying main's `ai_engine.sh` but not `codex_stall_guard.sh`: heal issues plan against `stable`, whose older guard rejected `--engine`. `plan.yml`, `clarify.yml` and `orchestrate_clarify_respond.yml` now copy the guard too.

| The numbers that matter | Value |
| --- | --- |
| `WORKFLOW_HEAL_PHASE_FAILURE_STREAK` (new) | default `1` |
| `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` | default `2` → `1` |
| API calls per phase report | 1 issue read, 1 identity read, 1 paginated comment read, 1 dispatch |
| Planning attempts lost per run before the fix | 3 of 3 (`exit_code=2`, `reason=no_transcript`) |

What this means for operators: expect a heal issue (or an occurrence comment on an open one) after the first failed clarify, plan, implement or review/autofix run. To leave the first failures to the stall poller as before, set `WORKFLOW_HEAL_PHASE_FAILURE_STREAK` or `WORKFLOW_HEAL_AUTOFIX_FAILURE_STREAK` to `2`; `WORKFLOW_HEAL_ENABLED=false` still turns heal off for a repository. Implement does not report a guard block (its label already reports), a failure turned into fix-up issues, or a `BLOCKED` verdict, and a cancelled run is never reported.

### For contributors

The reporter is `scripts/workflow_failure_heal_phase_report.sh` (log prefix `WORKFLOW_HEAL_PHASE_REPORT`); the streak and payload live in `scripts/workflow_failure_heal.py` (`phase_failure_streak`, `build_phase_failure_payload`, `build-phase-payload`). Streaks exclude untrusted comments, cancellations and successful implementation. The job runs separately because the intake cannot read the log of a job that is still running. It declares `permissions: {}` so it stays inside any caller's grant. `tests/test_ai_engine.py` fails when a workflow stages `ai_engine.sh` without the guard.
