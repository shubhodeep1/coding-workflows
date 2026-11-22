<!-- changelog: changed -->
- **Workflow failure heal intake sends fewer repeat Telegram warnings.** Reports from an unregistered repository warn once per repository per UTC day, and expected provenance rejections go out at DEBUG.

`Workflow Failure Heal Intake` (`.github/workflows/workflow-failure-heal-intake.yml`) used to send a WARNING for every report from a repository missing from `.github/ai/consumer_repos.json`; one such repository produced six identical WARNINGs overnight. The intake job now looks up an actions/cache marker keyed `heal-unregistered-<owner>_<repo>-<YYYYMMDD>` before running `scripts/workflow_failure_heal_intake.sh`. On a hit, the repeat report logs `unregistered_source_repo_alert level=DEBUG reason=already_warned_today` and sends at DEBUG. After a WARNING, the job saves the marker. Provenance rejections for a CI run that is not a default-branch push (`current_run_rejected:ci_not_default_branch_push`), and for cited runs that finished without failing (`current_run_rejected:not_failed`, or `no_verified_runs` where every rejection is `not_failed`), now send at DEBUG. Every other rejection reason still sends a WARNING.

| The numbers that matter | Value |
| --- | --- |
| Unregistered-repo WARNINGs | at most 1 per source repo per UTC day |
| New GitHub API calls | 0 |
| New jobs | 0 (two cache steps in the existing `intake` job) |

What this means for operators: the Telegram channel stops repeating the same unregistered-repo warning and no longer flags correct provenance rejections. Unregistered repositories stay unregistered, and every skip is still logged (`skip reason=unregistered_source_repo`, `skip reason=provenance_rejected`). With the default `ALERT_MSG_LEVEL=WARNING`, DEBUG messages are not delivered. If the cache lookup fails, the WARNING is sent anyway.

### For contributors

The workflow passes the lookup result to the intake as `WORKFLOW_HEAL_UNREGISTERED_WARNED_TODAY` (default `false`, which sends the WARNING). The intake writes the marker to the fixed path `WORKFLOW_HEAL_UNREGISTERED_MARKER` (`/tmp/workflow-heal-unregistered-warned`, the same path in every run because actions/cache versions entries by path) and the step output `heal_unregistered_warned=true` after a WARNING; the save step runs only on that output. Both cache steps use `continue-on-error: true` and run inside the heal intake queue. The new log lines are `unregistered_source_repo_alert level=<LEVEL>` and `provenance_rejected_alert level=<LEVEL>`.
