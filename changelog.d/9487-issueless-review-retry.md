<!-- changelog: added -->
- **Failed reviews of PRs with no linked issue are now retried.** When a review run fails before the editor (`reviewers_failed`, `provider_unavailable`, `sandbox_prepare_failed`), its comment promises a retry, but the stall poller only follows labelled issues, so an issue-less PR waited for a human. The orchestrator poller now has an issue-less review retry pass that re-dispatches such a PR's review once per head and failing review-support SHA, after a newer review-support (`@stable`) release is pinned.

The pass reads the conflict sweep's existing GraphQL batch, so it adds no read call. It acts only on open, non-draft, same-repository PRs with no closing reference to a pipeline-labelled issue and no `ai:review-blocked`, human-gate or latch label, and it trusts only markers the pipeline's own login wrote. The retry goes through the conflict sweep's default-branch dispatch, which skips a PR whose review is already running, and a `<!-- issueless-review-retry:v1 head=<sha> support=<sha> -->` comment records it. Every decision logs one `ISSUELESS_REVIEW_RETRY` line. For an issue-less PR the no-output comment now names this pass instead of the stall poller; its headline is unchanged.

| The numbers that matter | Value |
| --- | --- |
| New read calls per cycle | 0 |
| Re-dispatches per cycle (`ISSUELESS_REVIEW_RETRY_MAX_PER_CYCLE`) | 5 by default, `0` logs only |
| PR age window (`ISSUELESS_REVIEW_RETRY_MAX_AGE_DAYS`) | 7 days by default, `0` disables |

What this means for consumer repos: nothing to change. The poller runs in coding-workflows, and the retry waits until the consumer's pinned `review_autofix.yml` SHA differs from the one that failed. Refs shubhodeep1/tele-funtoken-msg-scoring#5219.
