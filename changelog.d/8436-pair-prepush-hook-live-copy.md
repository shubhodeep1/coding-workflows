<!-- changelog: fixed -->
- **The conflict resolver now handles conflicts on the live pre-push gate hook instead of parking the PR.** When `.claude/hooks/prepush_affected_tests.py` and its `workflow-templates/.claude/hooks/prepush_affected_tests.py` twin conflict with identical index stages, the model resolves only the template and the runner copies it byte-for-byte over the live hook, the same #6595 pairing path `.claude/hooks/pr_merge_status_guard.py` already used.

Before this fix the live copy was refused as a dot-directory path, so the whole conflict set failed closed with `sandbox_path_host_only` and no model ran. Integration-sync PRs escalate that reason at once, which is how project #8434's PR #8436 was parked in run 42360075171 on a merge with main that also touched five ordinary files. The pair is now listed in `PAIRED_LIVE_COPIES` in `scripts/review_untrusted_workspace.py`. A live copy whose template conflicts differently still fails closed.

| The numbers that matter | Value |
| --- | --- |
| Paired live hooks | 2 (`pr_merge_status_guard.py`, `prepush_affected_tests.py`) |
| Failing run | 42360075171 (PR #8436, `.claude/hooks/prepush_affected_tests.py`) |

What this means for operators: a merge that changes the pre-push gate hook on both sides no longer needs a manual merge. A PR already parked on this conflict still needs a resolved head pushed to resume, and the fix reaches the resolver once it is on the workflow support ref the review workflow runs from.
