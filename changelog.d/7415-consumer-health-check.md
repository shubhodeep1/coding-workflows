<!-- changelog: added -->
- **A daily check now watches every consumer repository and files an issue when one goes quiet.** Consumers whose AI workflows were switched off, whose poller stopped, or whose tracking issue was filling up used to go unnoticed until someone looked.

Once a day the orchestrator poller in coding-workflows runs `scripts/consumer_health_check.sh` over the repositories in `.github/ai/consumer_repos.json`. An AI workflow that GitHub switched off for inactivity is switched back on. A workflow switched off by a person, a poller with no run in 6 hours, a tracking issue with 2000 or more comments, or a repository the token cannot read each opens one `ai:consumer-health` issue here, with one Telegram WARNING. The issue closes itself when a later check no longer finds the problem.

| The numbers that matter | Value |
| --- | --- |
| Consumers checked | 13 (`.github/ai/consumer_repos.json`) |
| Poller silence threshold | 6 hours (`CONSUMER_HEALTH_POLLER_SILENT_HOURS`) |
| Tracking issue comment threshold | 2000 (`CONSUMER_HEALTH_TRACKING_COMMENT_LIMIT`; GitHub stops at 2500) |
| API calls per day | about 41 reads, plus writes only when something changes |

What this means for operators: a paused or stuck consumer shows up as an issue in this repository within a day, without anyone checking each repository. Workflows paused on purpose, like tele-funtoken-msg-scoring's, get an issue that stays open until they are switched back on or the repository is removed from the consumer list. Set `CONSUMER_HEALTH_CHECK_ENABLED=false` to turn the check off.
