<!-- changelog: fixed -->
- **Unblock judge fix-ups for a pull request now target the PR's `ai/issue-<n>` head branch.** A `descope` or `operator_step` fix-up issue for a trusted PR used to carry no Integration branch, so planning ran against `main` and blocked.

When the unblock judge files a standalone `descope` or `operator_step` fix-up for a trusted, same-repository pull request whose head branch is `ai/issue-<n>`, the new issue now carries `- Integration branch: \`ai/issue-<n>\``. `scripts/resolve_integration_ref.sh` then plans and implements the fix-up on the PR head, where the PR's changes live. Issue #9938, the fix-up for PR #9283 (head `ai/issue-9261`), was blocked by the planner because its ref resolved to `main`.

| The numbers that matter | Value |
| --- | --- |
| Head branches routed | `ai/issue-<n>` only |
| Verdicts affected | `descope`, `operator_step` (standalone, non-project PRs) |
| Incident | #9938 (fix-up for PR #9283) |

What this means for operators: unblock fix-ups for pipeline PRs plan on the PR's own branch instead of stalling on `main`. Untrusted or fork PRs, other head branches, issue items and project PRs (whose fix-ups go through the poller) are unchanged.

### For contributors

`scripts/unblock_judge.sh` adds `pr_head_ref` to the unblock context JSON, set only for a trusted PR (empty otherwise). `scripts/unblock_actions.py` validates it like `pr_base_branch` and drops an invalid value. The line is placed after `Why:` and before the provenance marker, as on the `reissue` path. If the head branch has been deleted, the resolver reports the declared branch as missing instead of falling back to `main`.
