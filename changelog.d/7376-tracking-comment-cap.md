<!-- changelog: fixed -->
- **Tracking issues no longer fill up to GitHub's 2500-comment cap, and a capped issue no longer stops the poller.**

tele-funtoken-msg-scoring#3928 reached 2500 comments on 24 September. After that every poll failed posting the judge verdict and stopped, which also stalled the repo's other projects, and its AI workflows were switched off on 28 September. The 2500 comments were 1791 state snapshots (every state write posts a new chain and none were removed), 447 `tg_cleanup` markers (the marker lookup read the oldest 30 comments, so it never found the existing marker), 163 identical "Integration self-healing resolved" comments and 99 others. The poller now deletes its own superseded state chains after each write and, when a write meets the cap, deletes first and retries once. A repeat of one of the pipeline's newest 20 comments updates that comment's `Repeated N times, last … UTC` line instead of posting again. The Telegram markers are found on the issue's newest page, and the comment-cap error is no longer retried or fatal.

| The numbers that matter | Value |
| --- | --- |
| State chains kept | newest, the one just written, and the newest whose status is not `failed` |
| Deletes per tracking issue per poll run | 50 (`ORCH_STATE_PRUNE_MAX_DELETES`) |
| Window for repeated comments | the pipeline's newest 20 comments, state and marker-only comments excluded |
| tele-funtoken-msg-scoring#3928 | 1779 of its 1791 state comments become deletable; the newest state is unchanged |

What this means for operators: a long-running project keeps a short, readable tracking issue and cannot lock itself out by hitting the comment cap. An issue already at the cap recovers on its own: the first state write deletes superseded chains and goes through. `ORCH_STATE_PRUNE_ENABLED=false` turns the deletion off.

### For contributors

`scripts/orchestrate_state_v2.py prune-plan` lists the deletable state comments; `prune_state_comment_chains` deletes them, computing the plan once per tracking issue per run from `COMMENTS`. `post_tracking_comment` keeps a per-run list of the pipeline's newest 20 comments and never edits a command (a body starting with `/`). `_is_gh_permanent_failure` in `scripts/gh_helpers.sh` matches the cap message, and the judge verdict and "Project Failed" posts log a warning instead of failing the run. `scripts/tg_helpers.sh` reads the issue's comment count and then its last page (`_tg_newest_comments_json`).
