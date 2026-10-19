<!-- changelog: fixed -->
- **A pull request that delivers only part of an issue no longer closes it.** The issue stays open with the remaining items listed, and clarification restarts for them.

Before this, when a clarification answer or auto-decision limited an issue to part of its scope (for example "this issue delivers Phase 1 only"), the implement PR still said `Closes <issue>`. When it merged, the issue was labelled `ai:merged` and closed with the rest of the work not started, and the activation re-check skipped the merge on purpose, so nothing tracked the rest (tele-funtoken-msg-scoring#4639).

Now:

- the plan lists the remaining items under a `## Deferred scope` section (one bullet each, at most 20) whenever an answer, an auto-decision or the plan's chosen `Scope-mode` limits the scope;
- implement opens such a PR with `Refs #<n>` instead of `Closes <url>`, and repeats the items under `<!-- ai:partial-delivery:v1 issue=<n> -->`;
- when the PR merges into the default branch or the issue's declared integration branch, and the PR comes from this repository, its head branch is `ai/issue-<n>` and its author is the `GH_PAT` account, `issue_pr_status.yml` leaves the issue open, posts the remaining items once as quoted untrusted text, sets `ai:clarification`, deletes the merged `ai/issue-<n>` branch when it still points at the merged head, and posts `/reclarify` with `<!-- ai:partial-delivery-restart:<n> -->`;
- clarify treats that restart as automated (like the PR-dependency release), so clarify-respond answers the follow-up questions without a person;
- the poller's merged-issue close sweep does not close such an issue either.

A missing, empty or malformed `## Deferred scope` section, an orchestrator-managed child, or any PR that is not bound to its issue as above closes the issue as before, and a full delivery still skips the activation re-check of an activation-fix merge. Set the repository variable `PARTIAL_DELIVERY_TRACKING_ENABLED=false` to turn this off. Consumers get the clarify trigger change with the next workflow sync.

### For contributors

`scripts/partial_delivery.py` parses the plan section and the PR body and renders the comment. Log prefix `PARTIAL_DELIVERY` (`mode=implement|merge|sweep`); clarify logs `AI_PHASE_GATE_V1 ... gate=partial_delivery_restart`.
