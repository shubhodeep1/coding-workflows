<!-- changelog: fixed -->
- **A PR no longer waits for a person once its `ai:needs-human` head latch is out of date.** The poller now removes `ai:needs-human` from a pull request when the label was set for one head and the PR has since moved to another.

Two latches set `ai:needs-human` for a single head: the workflow-failure-heal diagnosis of a review/autofix failure that another run cannot fix, and the review sweep's review-round cap. Both already said the way forward was a new head, but nothing removed the label when the head moved. PR #9839 was latched on 2026-11-20 at 11:15 for head `27dd193`. It then moved to head `bcd1d378`, passed its security pass, and the review-blocked judge chose "merge" five times, yet it still waited on a person to remove a label. The poller now checks each `ai:needs-human` PR on every tick (and on idle ticks before the unblock scan) and removes the label once the head has moved. It also posts a comment saying which head the latch was for and why it no longer applies. If the same failure comes back on the new head, the label comes back too.

| The numbers that matter | Value |
| --- | --- |
| Switch | `NEEDS_HUMAN_HEAD_LATCH_AUTO_RELEASE_ENABLED`, default `true` |
| Repositories | every repository (the staged-support release stays source-only) |
| Label binding window | the `labeled` event must follow the latch comment by at most 30 minutes, same login |
| Releases per head | 1 (`<!-- ai:needs-human-auto-release reason=... pr=<n> head=<sha> -->`) |
| API per tick | 1 listing; per latched PR 1 comments read, plus 1 `pulls/<n>` read for a head-bound latch and 1 events read once the head moved; per release 1 comments + 1 events + 1 `pulls/<n>` re-read, 1 label removal, 1 comments read after it (1 label add if a latch raced the removal), 1 comment |

What this means for operators: a PR parked for a head it is no longer on moves forward again by itself. Latches without a recorded head (round-cap comments posted before this change), PRs that also carry `ai:scope-blocked` or `ai:destructive-blocked`, and any label a person re-added stay human-cleared. Set the switch to `false` to keep every such latch for a person.

### For contributors

The review-round cap comment now carries `<!-- ai:review-round-cap-head:v1 head=<sha> -->` before its existing `<!-- ai:review-round-cap:v1 -->` marker (`review_sweep_round_cap_escalate` takes the head as an optional eighth argument, passed by `review_autofix_sweep.yml`). Tests: `tests/test_needs_human_head_latch_release.py`, `tests/test_review_sweep_round_cap.py`.
