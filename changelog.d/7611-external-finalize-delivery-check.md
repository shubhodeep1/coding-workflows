<!-- changelog: fixed -->
- **An orchestrator project whose integration branch moved after its final PR was merged elsewhere no longer reads as complete.** The poller now opens a fresh final PR for the commits that landed after the merge.

When an operator squash-merges a project's final PR and the security pass then merges more fix PRs onto the integration branch, the external-finalize path in `scripts/orchestrate_poll_process.sh` used to mark the project `complete`. The security pass had audited the integration branch tip, so the tracking issue said "passed", but those later commits never reached the default branch. In tele-funtoken-msg-scoring#4281, ten security fix cycles were stranded this way.

Before completing, the poller now compares the merged final PR's head with the current integration branch tip:

- the tip is the merged head, the branch was deleted, or the tip adds nothing the default branch lacks: the project completes as before, and the completion comment names the merged head, the tip and the SHA the security pass audited;
- the tip descends from the merged head and adds new content: the project stays open, the recorded final PR is cleared, one tracking comment names both SHAs, and the existing final-PR step opens a fresh PR;
- the tip does not descend from the merged head (history rewritten): the project stays open, and one tracking comment and one Telegram ERROR ask an operator to re-point the branch or deliver the changes by hand;
- the check cannot run (for example the merged head cannot be fetched): the project stays open and is checked again every tick, with one Telegram WARNING.

The check compares commits and content, not the compare API's "ahead by" count, so a squash-merged project with no later commits is not held. When the tip equals the merged head it makes no extra GitHub API call. Log prefix: `EXTERNAL_FINALIZE_DELIVERY`.
