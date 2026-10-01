<!-- changelog: fixed -->
- **The orchestrator's standalone conflict sweep no longer touches draft `claude/*` pull requests.** Draft `/implement-plan-claude` project integration PRs are synced with their base by their own chain at final merge.

Before this change, the sweep in `scripts/orchestrate_poll_process.sh` tried `update-branch` on every conflicted open PR and then dispatched a review workflow. That included draft project PRs such as #4648, #4598 and #4593, so it posted a "Standalone PR #N has merge conflicts" alert for each of them on every poller tick. It now skips a PR when its head is `claude/*` and it is a draft, reading the `draft` flag from the PR object it already fetches. Non-draft `claude/*` PRs and every other branch keep the existing behaviour.

What this means for operators: no more repeated conflict alerts or wasted review runs for draft Claude project PRs, and no extra API calls.
