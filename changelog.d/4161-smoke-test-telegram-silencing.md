<!-- changelog: fixed -->
- **A release-gate run now produces one Telegram message, its final pass/fail.** The per-phase pings from the smoke-test pipeline no longer leak past the gate's `SILENT` setting.

`test-and-mark-stable.yml`, and `promote-main-to-stable.yml` which dispatches it, were always meant to send a single combined notification from the `notify` job. Every pipeline workflow the smoke fixture triggers already exported `ALERT_MSG_LEVEL=SILENT` on detection, but each Telegram step declared its own step-level `ALERT_MSG_LEVEL` from repo vars, and a step's `env:` block overrides the job-level value in GitHub Actions. Operators therefore still received "Clarification required", "Plan awaiting approval", orchestrator and review pings during every release. The 18 step-level declarations across `clarify.yml`, `plan.yml`, `implement.yml`, `review_autofix.yml`, `orchestrate.yml`, `orchestrate_poll.yml` and `orchestrate_clarify_respond.yml` now read the job env first. `validate.yml` gains an optional `alert_msg_level` input (the gate passes `SILENT` to its standalone validate smoke), and `check_failure_triage.yml` runs silent for PRs labelled `e2e-smoke-test`.

| The numbers that matter | Value |
| --- | --- |
| Step-level declarations fixed | 18 across 7 workflows |
| New `validate.yml` input | `alert_msg_level` (default empty, honours `vars.ALERT_MSG_LEVEL`) |
| Contract test | `tests/test_smoke_alert_silencing_contract.py` |

What this means for operators: a release or promote run posts the `Release … SUCCEEDED` / `FAILED` message and nothing else. Production issues are unaffected, since only the smoke fixture sets the job-level `SILENT` value; `vars.ALERT_MSG_LEVEL`, `PR_PROCESSED_ALERT_LEVEL` and `CONFLICT_RESOLVED_ALERT_LEVEL` keep their existing meaning outside smoke runs.

### For contributors

Consumer repos receive the updated `ai-validate.yml` wrapper on the next `@stable` sync; the new input is optional, so wrappers that predate it keep working. Any new Telegram step in a smoke-silencing workflow must declare `ALERT_MSG_LEVEL: ${{ env.ALERT_MSG_LEVEL || … }}`; the contract test fails otherwise.
