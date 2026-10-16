<!-- changelog: fixed -->
- **Planning no longer fails on a CI-triage issue whose pull request is already merged or closed.** Plan now treats a `reason=pr_not_open` refusal from CI-triage branch routing the way clarify already does, and lets its superseded-PR check decide what happens next.

Until now plan's "Resolve integration ref" step failed on every CI-triage routing refusal, including `pr_not_open`. That stopped the run before the "Close superseded CI-repair issue (merged PR)" step (#7216), the one step built for this case, could act. Stall recovery then retried plan, and every retry failed the same way (multi-user-ai-agent#264, run 38862023746). Now the step replays the refusal as a notice, writes an empty ref and the new step output `pr_not_open=true`, and the superseded-PR step settles the issue:

| Pull request state | What plan does |
| --- | --- |
| Merged, check passed on its final head | Closes the issue as superseded (unchanged) |
| Merged, check not passed, base is not the default branch | Plans on the pull request's base branch; skips planning if that checkout fails |
| Merged, check not passed, base is the default branch | Plans on the default branch |
| Closed without merging, or anything the check cannot verify | Skips planning with `PLAN_SUPERSEDED_MERGED_PR ... outcome=skip reason=pr_not_open_unresolved detail=<reason>` and one WARNING |

What this means for operators: a triage issue whose pull request is gone is never planned against the wrong branch, and it no longer produces a failed run on every retry. A pull request that closed without merging still needs a person to close the issue. Every other routing refusal, and a rate-limited resolver while routing is on, still fails the step. With `CHECK_TRIAGE_BRANCH_GUARD_ENABLED=true` the check-triage branch guard still blocks these issues, because the step records `resolver_status=failed`.
