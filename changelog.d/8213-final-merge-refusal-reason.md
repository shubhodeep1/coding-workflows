<!-- changelog: fixed -->
- **The final integration merge now waits on failing ruleset-required checks instead of burning its merge attempts.** When a merge is still refused, the tracking issue records why.

Before this change, the final-merge gate read required checks only from classic branch protection, and it judged only check-runs. A required commit status from a repository ruleset was invisible to it. In shubhodeep1/fun-token-multi-chain#579, that status was `coding-workflows/workflow-integrity`. The poller tried the merge three times, GitHub refused each one, and the project parked with "Last recorded error: ... (state=open, mergeable=true)".

Now, when GitHub reports the final PR as `mergeable_state=blocked`, the poller reads the base branch's ruleset-required checks. If one is failing, pending or missing, it holds the merge without spending the attempt budget and logs `[final-merge] PR #N is blocked by ruleset-required check(s): ...`. The `ORCH_FINAL_MERGE_INELIGIBLE_ALERT_HOURS` alert (default 6h) lists those blockers as `ruleset: <context> (<state>)`. If a merge is still refused, `final_merge_error` and the "Final merge blocked" comment include the `mergeable_state` and GitHub's refusal text.

| The numbers that matter | Value |
| --- | --- |
| Extra API calls on an unblocked final PR | 0 |
| Extra API calls on a `blocked` final PR, per poll | 2, or 3 when a required context has no commit status |
| Refusal text kept | first 600 characters, one line |

What this means for operators: a project whose final PR is held by a ruleset check stays in progress and raises the stuck-merge alert naming the check. It no longer parks with `final_merge_exhausted` and a misleading "mergeable=true". If the ruleset lookup fails, the poller warns and tries the merge as before.
