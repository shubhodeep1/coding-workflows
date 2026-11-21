<!-- changelog: changed -->
- **The needs-human digest now also lists items that carry `ai:needs-human` without being parked.** A separate section shows every open issue or PR with the label that the unblock judge has not parked, so the digest no longer reads "Nothing is waiting." while items wait for a person.

The digest issue (body marker `<!-- ai:needs-human:v1 -->`) used to list only items whose unblock-judge rounds were spent. Labels set by other paths stayed invisible there: the review-round cap, the workflow-failure-heal non-retryable diagnosis, a stall escalation, a hand-applied label. On 2026-11-20 the digest in this repository read "Nothing is waiting." while #9283, #9261, #9839 and #9821 all carried `ai:needs-human`. The poller's existing digest prune now fills a "Also labelled `ai:needs-human` (not parked)" section from the listing it already reads. The section is display only: its lines carry no entry marker, so the unblock scan's parked list and the judge's work on those items are unchanged.

| The numbers that matter | Value |
| --- | --- |
| Section marker | `<!-- ai:needs-human:unparked:v1 -->` |
| Lines shown | at most 100, then "...and N more." |
| Extra API calls | none for a non-empty digest; an empty digest now makes the one paginated `ai:needs-human` listing per tick |
| Body writes | only when the list changed |

What this means for operators: the digest is one place to see every item waiting on `ai:needs-human`. Parked items keep their full entry with stop, reason and link; the not-parked items point to their own newest pipeline comment.
