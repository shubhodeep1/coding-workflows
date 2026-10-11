<!-- changelog: fixed -->
- **`/reclarify` now restarts clarification from any line of a comment.**

A trusted comment that answers the blocker first and puts `/reclarify` on a later line, for example the last line, used to skip the clarify job silently, leaving the issue blocked. Clarify now starts when any line of the comment begins with `/reclarify`. On a later line the command counts only when the comment has no `<!-- ai:` automation marker and no plan-comment trailer, so the pipeline's own comments (the plan comment now ends with `<!-- ai:plan-proposal:v1 -->`) never restart clarification. A `/reclarify` inside a sentence still does nothing. As a backstop, the poller posts one advisory comment and a Telegram WARNING when an open `ai:blocked` issue's newest trusted human reply goes unrouted for 15 minutes (`RECLARIFY_UNROUTED_DETECT_ENABLED`, `RECLARIFY_UNROUTED_GRACE_MINUTES`, `RECLARIFY_UNROUTED_MAX_AGE_HOURS`). Consumer repos receive the new intake at the next `@stable` sync of `ai-clarify.yml`.

What this means for operators: you can put `/reclarify` after your answer, and a reply that did not resume a blocked issue is now reported instead of stalling unnoticed.
