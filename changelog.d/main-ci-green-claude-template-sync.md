<!-- changelog: fixed -->
- **`main` CI is green again, and a template-only change under `workflow-templates/.claude/` now gets synced to this repo's own `.claude/` copy automatically.** `main` had failed CI on every push since 2026-10-03 09:51 UTC.

Two merged PRs updated a template under `workflow-templates/.claude/` without the live copy under `.claude/`: #6133 (the merged-PR guard hook `pr_merge_status_guard.py`) and #6176 (the `audit-plans`, `apply-url`, `implement-plan-ai` and `implement-issue-claude` commands). The pipeline's editors cannot edit `.claude/**`, so AI fixes leave the live copy behind. This change updates the hook and three live commands; `audit-plans.md` has no change in this PR. A new CI test fails for missing or differing live files unless they are listed in `.github/ai/claude_template_divergence.json` with a reason. The new `sync-claude-live-copies.yml` workflow opens a PR on `ai/sync-claude-live-copies` for template-only changes, including new files and changes missed by a failed or superseded run, without overwriting newer live-file edits.

| The numbers that matter | Value |
| --- | --- |
| Failed `CI` push runs on `main`, 2026-10-03 09:51 to 2026-10-04 17:56 UTC | 35 of 35 |
| Live copies updated in this change | 4 (1 hook, 3 commands) |
| Command files allowlisted as maintained separately | 6 |

What this means for operators: CI on new PRs stops failing for reasons the PR did not cause. If a sync PR from `ai/sync-claude-live-copies` appears, it copies template content and executable mode into `.claude/`; it goes through the normal review. To keep a file intentionally different, add it to `.github/ai/claude_template_divergence.json` with a reason.
