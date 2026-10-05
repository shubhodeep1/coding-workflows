<!-- changelog: fixed -->
- **Template/live `.claude/` parity failures are corrected, template-only changes now sync to this repo's live copy automatically, and rejected relay requests no longer break their client connection.** This addresses the CI failures seen on `main` since 2026-10-03 09:51 UTC.

Two merged PRs updated a template under `workflow-templates/.claude/` without the live copy under `.claude/`: #6133 (the merged-PR guard hook `pr_merge_status_guard.py`) and #6176 (the `audit-plans`, `apply-url`, `implement-plan-ai` and `implement-issue-claude` commands). The pipeline's editors cannot edit `.claude/**`, so AI fixes leave the live copy behind. This change updates the hook and three live commands; `audit-plans.md` has no change in this PR. CI prepares eligible template-only command copies in its disposable checkout for PRs targeting `main`, but always checks security hooks and `settings.json` against committed live copies; push CI still checks committed parity. A new CI test fails for missing or differing live files unless they are listed in `.github/ai/claude_template_divergence.json` with a reason, and `sync-claude-live-copies.yml` opens a PR for template-only drift without overwriting newer live-file edits. The Anthropic relay also drains a valid, bounded request body before rejecting invalid authorization or an oversized forwarded header so `http.client` receives the intended HTTP 400 instead of `BrokenPipeError`.

| The numbers that matter | Value |
| --- | --- |
| Failed `CI` push runs on `main`, 2026-10-03 09:51 to 2026-10-04 17:56 UTC | 35 of 35 |
| Live copies updated in this change | 4 (1 hook, 3 commands) |
| Command files allowlisted as maintained separately | 6 |
| Anthropic relay focused suite | 31 tests passing |

What this means for operators: CI on new PRs no longer fails from these template/live parity mismatches or the relay's invalid-authorization regression. If a sync PR from `ai/sync-claude-live-copies` appears, it copies template content and executable mode into `.claude/`; it goes through the normal review. Symlinked template or live paths fail rather than copying checkout credentials or writing outside `.claude/`. To keep a file intentionally different, add it to `.github/ai/claude_template_divergence.json` with a reason.
