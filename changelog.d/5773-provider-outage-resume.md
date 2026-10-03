<!-- changelog: fixed -->
- **A model-provider outage no longer blocks pull requests, and the pipeline resumes on its own when the provider recovers.** A review/autofix run whose model calls failed because the provider is down is now named `provider_unavailable` instead of a per-PR failure.

On 2026-09-30 the OpenRouter account ran out of credits for about six hours. The identical-failure cap tripped on 35 PR heads, 36 PRs and issues were labelled `ai:review-blocked`, Claude fixers were started and put on hold, and a heal issue was filed for the stable release. Recovery took a human for every step. Now an outage run labels nothing, counts toward no cap or heal streak, starts no Claude fixer, and posts an "AI review/autofix paused: model provider unavailable" comment. The first such failure opens one `ai:provider-outage` marker issue in coding-workflows and sends one Telegram alert naming the provider, the HTTP status and the key. The `provider-outage-probe` job of `review_autofix_sweep.yml` then probes the provider every 30 minutes and pauses the sweep's review dispatches. When the provider answers again, it re-dispatches the paused reviews (here and in registered consumers), removes only the outage's `ai:review-blocked` labels, releases only outage-caused holds, closes the marker, and sends one "recovered" alert.

| The numbers that matter | Value |
| --- | --- |
| Errors that classify on sight | 402 / "Insufficient credits", 401 on the provider key |
| Errors that classify only when no reviewer succeeded | 429, 5xx |
| Probe cadence and cost | every 30 minutes (`*/30`), one 1-token completion while a marker is open; one REST read otherwise |
| New repo variables | `PROVIDER_OUTAGE_PROBE_ENABLED` (`true`), `PROVIDER_OUTAGE_PROBE_MODEL` (`XPOLL_SUMMARISER_MODEL` or `openai/gpt-6-luna`), `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED` (`false`) |
| New label | `ai:provider-outage` (excluded from clarify and the Claude issue route) |

What this means for operators: when the provider account runs dry, top it up and do nothing else. The marker issue says what is paused. The probe closes it and resumes the reviews within 30 minutes of recovery. A release run that failed during the outage is reported in the recovered alert, and re-run only when `PROVIDER_OUTAGE_RELEASE_RERUN_ENABLED=true`. Claude sessions wait on an outage through their hourly check-ins; they never ask for credits.

### For contributors

The classifier lives in `scripts/workflow_failure_heal.py` (`detect_provider_outage`, `autofix-failure-fingerprint --provider-log-dir`, `provider-outage-detect`). The marker, probe and resume live in `scripts/provider_outage.py` (`record` from the heal intake, `tick` from the sweep, `status` for diagnosis). The `.claude/scripts/check_in_status.py` change, the `provider-unavailable` wait state, ships through its `workflow-templates/.claude/` twin and reaches `.claude/` with the twin sync.
