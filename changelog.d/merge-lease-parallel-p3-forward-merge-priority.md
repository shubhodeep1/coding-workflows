<!-- changelog: changed -->
- **Forward-merge PRs from `stable` to `main` no longer wait behind ordinary feature PRs for the merge lease.** A PR whose head branch is in the same repository and starts with `auto/forward-merge-stable-` now ranks with the `MERGE_TRAIN_PRIORITY_LABELS` group (`ai:workflow-heal`, `ai:security`) in the lease queue: when the lease is free, the merge-train tick grants it to such a waiter first, and the update-mode lease gate makes a plain PR wait behind it. Within that group the lowest PR number still wins, so a priority-labelled PR with a lower number goes first. A fork PR whose branch has the same name gets no priority. The head branch is read from the PR listings the lease already fetches, so no GitHub API call is added, and with no forward-merge PR waiting the order is exactly as before.

`MERGE_LEASE_PRIORITY_HEAD_PREFIXES` (default `auto/forward-merge-stable-`, a comma-separated list of literal branch prefixes) sets which branches get the priority; `none` or `off` turns it off. It can be set as a repo variable, with the same default as `scripts/pr_checks_lib.sh`; a repo variable set to an empty string falls back to the default, so use `none` or `off` there.

What this means for consumer repos: after the next `@stable` sync, a forward-merge fallback PR takes the next free lease on `main` instead of queueing by PR number. Nothing needs to be done.

Refs #9781
