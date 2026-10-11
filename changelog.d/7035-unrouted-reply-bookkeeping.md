<!-- changelog: fixed -->
- **The "blocked issue has an unrouted reply" warning no longer fires on the pipeline's own comments.**

The poller's check for blocked issues whose human reply never resumed the pipeline treated the pipeline's own bookkeeping comments as human replies. These were the bare `<!-- tg_cleanup:N -->` comment posted right after each blocking alert, and the `<!-- workflow-failure-heal:occurrence -->` notes. All 23 warnings posted on 2026-10-09 and 2026-10-10 were these false positives, and each one added an advisory comment to the issue and a Telegram WARNING. A comment now counts as a reply only when it has visible text and carries none of the pipeline's markers. A real reply followed by a bookkeeping comment is still flagged, now under its own comment id.

| The numbers that matter | Value |
| --- | --- |
| False-positive warnings on 2026-10-09 and 2026-10-10 | 23 of 23 |

What this means for operators: an unrouted-reply WARNING now points at a reply someone actually wrote.
