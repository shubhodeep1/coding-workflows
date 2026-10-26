<!-- changelog: fixed -->
- **A refused final integration merge now records why GitHub refused it.** The tracking issue shows the failing required check, not just `mergeable=true`.

When `gh pr merge` refuses the final integration PR, the poller records two things: GitHub's refusal text and the PR's `mergeable_state` (for example `blocked`). Both go into `final_merge_error`, the `## ⚠️ Final merge blocked` tracking comment, and the poller log. Before this fix the reason was thrown away. In shubhodeep1/fun-token-multi-chain#579, all three attempts ended with "could not be merged automatically (state=open, mergeable=true)". The real blocker was a failing required ruleset status, `coding-workflows/workflow-integrity`. The "Final integration merge could not complete" comment prints this field as "Last recorded error".

What this means for operators: when a project parks with `final_merge_exhausted`, the tracking issue names the check or policy that blocked the merge. You no longer have to open the PR to find it.
