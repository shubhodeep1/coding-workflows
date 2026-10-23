<!-- changelog: fixed -->
- **The heal intake no longer loses reports that arrive in bursts.** Intake runs shared one concurrency group, and GitHub cancels a group's waiting run when a newer one is queued. A burst of heal reports therefore kept only the first and the last, and the rest were cancelled before any job read them, with no log line or alert.

`.github/workflows/workflow-failure-heal-intake.yml` now gives every run its own concurrency group. Each run waits its turn in a first-come, first-served queue (`scripts/workflow_failure_heal_queue.sh`): it posts a ticket comment on the `ai:heal-intake-queue` issue, waits until no older live ticket remains, and deletes the ticket when it finishes. Tickets of runs that have completed, been re-attempted or passed the lease no longer hold the queue. The hourly recovery scan waits in the same queue. Time spent waiting does not count against `WORKFLOW_HEAL_REPORT_MAX_AGE_SECONDS`. If the queue cannot be used, or a run has waited 80 minutes, the run goes ahead without waiting and sends one WARNING. The report is never dropped.

| The numbers that matter | Value |
| --- | --- |
| Intake runs cancelled before a job started, 2026-10-21 | about 15 |
| Reports in one burst, 16:11:13–16:12:18 UTC | 4 arrived, 2 cancelled unread |
| Poll interval / maximum wait / ticket lease | 20 s / 4800 s / 10800 s |
| Intake / recovery-scan job timeout | 45 → 130 min / 95 → 175 min |
| API calls for a run that finds the queue empty | 5 |

What this means for operators: a busy hour no longer loses heal reports. Waiting runs hold a runner, and their `WORKFLOW_HEAL_QUEUE` log lines show their place in the queue. `HEAL_INTAKE_QUEUE_ENABLED=false` brings back the shared group, which cancels waiting runs again.

### For contributors

The ordering and staleness rules live in `workflow_failure_heal.py heal-queue position` and are covered by `tests/test_workflow_failure_heal_queue.py`. Only tickets written by the GH_PAT login count, and the ticket marker must be the comment's last line. A run that cannot use the queue goes ahead unlocked: the worst case is then two intakes deciding at once, which can at most duplicate a heal issue.
