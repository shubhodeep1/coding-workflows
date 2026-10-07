<!-- changelog: security -->
- **The orchestrator's review-blocked judge can no longer push edits to workflows, scripts, prompts, Claude hooks or consumer templates.** Security finding `review-blocked-protected-file-write` (high) is closed. Refs #3576.

When a PR is stuck in `ai:review-blocked`, the poller asks a judge to unblock it, and the judge may choose `fix` and edit files on the PR branch. The judge reads untrusted PR comments. Before this change, with `ALLOW_WORKFLOW_EDITS` at its default of `true`, a fix could change any file already in the PR, including `.github/`, `scripts/`, `prompts/`, `.claude/` and `workflow-templates/`. The check looked at file names, not content, and the poller then pushed the change with its token. Now any fix that stages a path under those directories is rejected, whatever `ALLOW_WORKFLOW_EDITS` is set to. The rejection works like other scope rejections: no commit or push, one review-blocked retry used, and a Telegram WARNING.

The judge prompt now says protected-path fixes are unavailable. For findings in those directories, the judge chooses `merge_with_followup` when the PR is shippable, otherwise `close_and_reissue`. The repair then goes through the normal implement and review pipeline.

| The numbers that matter | Value |
| --- | --- |
| Protected-path fixes the poller judge can push | 0 (was: any protected file in the PR, by default) |
| New environment variables or GitHub API calls | 0 (a rejected fix now skips one PR file listing) |

What this means for operators: `REVIEW_BLOCKED_FIX_SCOPE_REJECTED` lines can now carry `reason=protected_path_forbidden`. Repos with `ALLOW_WORKFLOW_EDITS=false` still see `reason=workflow_edits_disabled`.

### For contributors

The standalone review-blocked judge (`scripts/review_rb_judge.sh`) fix path is unchanged and will be handled separately.
