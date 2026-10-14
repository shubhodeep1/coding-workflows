<!-- changelog: changed -->
- **CI-triage issues for a failing pull request are now planned and implemented on that pull request's branch by default.** `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` defaults to `true`.

A check that fails on an AI pull request files an `ai:check-triage` issue. With routing off, clarify, plan, implement, clarify-respond and validate checked that issue out on `main`, where the failing code does not exist, so planning blocked: #7324 (for PR #7249, branch `ai/issue-7228`) and #7332 (for PR #7259, branch `ai/issue-7257`) both stopped that way on 13 October. Routing now runs unless the variable is `false`. Every existing check still applies before a branch is used: the `ai:check-triage` label, the marker header, an OWNER/MEMBER/COLLABORATOR author, the body's PR line, an open same-repository PR with the same head branch, and the branch itself. Any failed check still exits `3` and the phase fails closed instead of using the default branch.

| The numbers that matter | Value |
| --- | --- |
| Workflows whose default changed | `clarify.yml`, `plan.yml`, `implement.yml`, `orchestrate_clarify_respond.yml`, `validate.yml` |
| Values that keep routing on | unset, empty, `true`, `1`, `yes`, `on` |
| Values that turn it off | `false`, `0`, `no`, `off`; anything else also turns it off with a `::warning::` |

What this means for operators and consumer repos: after the next `@stable` sync, CI-triage issues get fixed on the failing pull request's own branch without a repository variable. To keep the old behaviour, set `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED=false`. The known gap is unchanged: a fix merged into the routed branch does not close the triage issue. Since #7283, `ci.yml` runs on pull requests into that branch and the auto-merge wait requires their `lint`. With routing on, a resolver that cannot be staged, pinned or found now also fails the phase when the variable is unset.

Two exceptions keep the default on safe. Clarify still runs on the default branch when the triage issue's pull request has already merged or closed, so the existing check-triage resolution gate (#7095) can close the issue with evidence; plan and implement still refuse it. While routing is on, clarify, plan and clarify-respond now stop on a rate-limited resolver (exit `75`) instead of using the default branch, as implement and validate already did.
