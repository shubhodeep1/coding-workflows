<!-- changelog: added -->
- **A CI-triage issue can now be planned and implemented on the branch of the PR whose check failed, behind `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED` (on by default; `false` turns it off).** Unblock fix-up for #6815.

Before this, every issue phase resolved a triage issue to the default branch: `scripts/resolve_integration_ref.sh` reads only `Integration branch:`, `Target branch:` and `Tracking issue:` lines, and the triage body is not allowed to carry them. #6815 was therefore planned on `main`, where the failing code from PR #6564 does not exist, and blocked.

With the variable on, the resolver routes an issue to the PR's head branch, but only after it verifies all of these:
- the `ai:check-triage` label and the exact four-line `check-failure-triage` marker header;
- an OWNER, MEMBER or COLLABORATOR issue author;
- a `Pull request:` line whose URL and number match the marker and whose branch matches the PR's head branch;
- the live PR: open, head and base in this repository;
- the branch itself.

If any check fails, the resolver exits `3`. Clarify, plan, implement, clarify-respond and validate then fail the step with `::error::CI_TRIAGE_PR_BRANCH_ROUTING ... outcome=refused reason=<token>` instead of falling back to the default branch. Every other resolver failure keeps its default-branch fallback (since #7283, a resolver that cannot be staged, pinned or found fails the step while the variable is on), and an explicit `Integration branch:` line still wins.

| The numbers that matter | Value |
| --- | --- |
| Behaviour change with the variable set to `false` | none |
| Extra GitHub API calls per phase on a routed triage issue | 2 (PR read, branch read) |
| Fork PR branches that can be selected | 0 |

What this means for operators:
- Re-run planning for #6815 (`/reclarify` or `/answer`); set `CI_TRIAGE_PR_BRANCH_ROUTING_ENABLED=false` to keep the default branch.
- Known gap: a fix PR merged into the routed branch does not close the triage issue. Since #7283, `ci.yml` runs on PRs into that branch and the auto-merge wait requires their `lint`.
