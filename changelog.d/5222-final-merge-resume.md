<!-- changelog: fixed -->
- **`/reclarify` now resumes an issue-mode Claude project blocked at `final-merge` after the manual merge of its final PR closed the issue.** The resumed session runs verify-activation instead of the project stopping at the merge.

An issue-mode `/implement-plan-claude` project that stops at a `final-merge` stage asks for its final PR to be merged by hand and then `/reclarify` on the issue. The final PR carries `Fixes #<N>`, so the merge closed the issue first, and clarify skipped every comment on a closed issue (`reason=issue_closed outcome=skip`). On #5119 the `/reclarify` posted five seconds after the merge was dropped, and verify-activation had to be started by hand. Now clarify (`.github/workflows/clarify.yml`, `Decide clarify route`) and the intake (`scripts/claude_issue_intake.sh`) route that `/reclarify` to Claude with reason `final_merge_resume`, and `/implement-issue-claude` continues the closed issue instead of stopping. The issue is not reopened. The resumed session removes `ai:claude-blocked` and picks the project up from its log.

| The numbers that matter | Value |
| --- | --- |
| Conditions (all required) | issue closed with `ai:claude` + `ai:claude-blocked`, no `ai:codex`; latest trusted-`User` `ai:claude-blocked:v1` comment has a `Stage:` line starting `final-merge`; trusted `/reclarify` after it |
| New clarify log line | `AI_PHASE_GATE_V1 phase=clarify gate=route reason=final_merge_resume outcome=handoff` |
| New clarify step output | `final_merge_resume` (`true` / `false`) |
| New intake authorization reason | `final_merge_resume` |
| Extra GitHub API calls | 1 paginated comments read in clarify, only for a closed `/reclarify` issue carrying both labels |

What this means for operators: after merging a blocked project's final PR by hand, comment `/reclarify` on the issue, even though the merge closed it. Every other comment on a closed issue is still skipped, and the intake still refuses other closed issues with `issue_closed`.

### For contributors

`scripts/claude_issue_route.py` holds the rule in `final_merge_resume(issue, comments)`. The CLI is `final-merge-resume --issue-json --comments-json`, and `authorize_target` calls the same function. Blocked comments from anyone other than an `OWNER`, `MEMBER`, or `COLLABORATOR` `User` are ignored. The stage is read from the first `Stage:` / `**Stage:**` line, which `/implement-plan-claude` issue-mode stops now must carry. A failed comments read keeps the skip. The trigger stays `reclarify`, so the `claude_issue.v1` payload and the queue format are unchanged.
