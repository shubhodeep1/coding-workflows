<!-- changelog: security -->
- **`permission_prompts.py duplicate-check` no longer lets an outsider's issue or fork PR authorize closing an `ai:permission-prompt` issue.** The duplicate target must now be written by an owner, member, or collaborator, and its fix must be a same-repository pull request by one.

Security audit finding #5809 (A01:2021 Broken Access Control, high) showed that in a public repository anyone could open a target issue with a forged class marker and a fork PR whose title names it, and `duplicate-check` would print `"eligible": true`, so the unattended `/implement-issue-claude` session closed a genuine pipeline-filed issue with no verified fix. The check now refuses a target whose `author_association` is not `OWNER`, `MEMBER`, or `COLLABORATOR`, a fix PR whose head repository is missing or differs from its base repository, and a fix PR by any other account. An untrusted target's `target_class` and `target_occurrences` evidence fields are now `null`. CLAUDE.md §23.I condition 2 states the rule.

| The numbers that matter | Value |
| --- | --- |
| Trusted associations (target and fix PR) | `OWNER`, `MEMBER`, `COLLABORATOR` |
| `duplicate-check` REST reads | still at most 5 (no new call) |
| New refusal reasons | 3 (untrusted target, fork or deleted-fork PR, untrusted PR author) |

What this means for operators: a duplicate whose target or fix comes from outside the repository's collaborators is no longer closed automatically; the session asks on the issue with the `ai:claude-blocked` label instead, as it did before #4867.

### For contributors

The change lands in the `workflow-templates/.claude/scripts/permission_prompts.py` twin (interim twin-first rule, #4785) and reaches `.claude/scripts/` at the twin sync. The trust set is `check_in_status.FIX_CLAIM_TRUSTED_ASSOCIATIONS`, the one fix claims already use. `tests/test_permission_prompt_duplicates.py` covers each refusal and the finding's scenario.
