# Deploy-Activation Log — Verified security-pass skip for issue-mode projects (`security_pass_skip.py`)

- Reference: docs/completed/issue-4623-verify-security-pass-skip-plan.md   (+ source issue #4623; final PR #4635, merged 2026-09-28 01:13 UTC as `d82b8d1e`; phase PR #4645, conformance fix #4663, completion #4673; progress log docs/implement-plan/issue-4623-verify-security-pass-skip.md)
- Deploy target: shubhodeep1/coding-workflows (+ the 13 consumers in `.github/ai/consumer_repos.json` via `@stable`)
- How it runs: on demand. `/implement-issue-claude` step 6 (`.claude/commands/implement-issue-claude.md:35`) runs `.claude/scripts/security_pass_skip.py` in every issue-mode session. In coding-workflows those sessions check out `main` and are started by the hourly `Claude issue pickup` routine. Consumers get the script and the command through `workflow-templates/.claude/`, synced by `ai-update-workflows.yml` → `update_workflows.yml@stable` (daily cron `0 4 * * *` plus the `@stable` `repository_dispatch`).
- Status: IN_PROGRESS
- Last updated: 2026-09-28
- Last note: 2026-09-28: step 3 checked on the operator Mac: `stable` branch and tag still `d58d7bc` (v1.29.12) / `fade4be9`, no `security_pass_skip.py` and no `workflow-templates/.claude/commands/implement-issue-claude.md` on either. Waiting on docs/deploy-activation/pr-4443.md steps 9a/9 (still BLOCKED at step 8).

## Runbook
1. [x] Prereqs: Homebrew, git, gh, jq, `gh auth login` (repo scope), clone or refresh `~/src/coding-workflows`   — done 2026-09-28: git 2.55.0, gh 2.101.0, jq 1.8.2 already installed; `gh api user` = shubhodeep1; `~/src/coding-workflows` fast-forwarded to main `677e8f6`; both `security_pass_skip.py` paths present
2. [x] Verify live in coding-workflows (read-only, run in the session)   — done 2026-09-28: on `main` 5206437, `security_pass_skip.py --issue 4623` → `{"skip": true, "label": "ai:security", …}` exit 0; `--issue 3576` → `{"skip": false, "label": null, "reason": "no skip label"}` exit 0
3. [ ] Wait for docs/deploy-activation/pr-4443.md steps 9a/9 (lift `PROMOTE_CYCLE_ENABLED=false`, promote main → `@stable`); no operator action for this project. Then verify on `stable` content, not ancestry: `workflow-templates/.claude/scripts/security_pass_skip.py` and the `security_pass_skip.py` reference in `workflow-templates/.claude/commands/implement-issue-claude.md`, on both the `stable` branch and the `stable` tag
4. [ ] Verify the consumer sync: after the next `update_workflows` run in one consumer, its default branch carries `.claude/scripts/security_pass_skip.py`, the updated `.claude/commands/implement-issue-claude.md`, and the `security_pass_skip.py` allow entry in `.claude/settings.json`
5. [ ] Verify LIVE; mark auto-decisions AD-1…AD-7 `confirmed` in the progress log

## Notes
- 2026-09-28: verify-activation 1/3 (activation scope) = DORMANT, no fix PR, no new auto-decisions (issue #4623 progress comment). Live on `main`; the 13 consumers pin `@stable` v1.29.12 (`d58d7bc`, tag `stable` = `fade4be9`), which has no `security_pass_skip.py` on the branch or the tag (checked 2026-09-28).
- No repo variable, secret, or supervisor gates this project (plan "Rollout": no flag, the failure mode is running the audit). The only gate is `@stable` promotion (§14).
- Promotion is paused on purpose: `PROMOTE_CYCLE_ENABLED=false` (read at `promote-main-to-stable.yml:138`, default `true`) belongs to docs/deploy-activation/pr-4443.md step 8b (operator Q6: A) and is lifted at its step 9a. This runbook never re-enables it and never runs a manual promotion; it waits for pr-4443's step 9, as docs/deploy-activation/plan-heal-deterministic-autofix-failures-plan.md step 3 does.
- Consumers are on Codex until the same promotion carries #4443 (pr-4443 step 6: `AI_ISSUE_IMPLEMENTER` unset → `claude` after promotion), so no consumer runs `/implement-issue-claude` before it has this script.
- `stable` is not a descendant of main (promotions are non-ancestral), so step 3 checks file content.
- Auto-decisions AD-1…AD-7 (progress log `## Auto-decisions`) are all `pending review`; they are listed in the opening message and confirmed at LIVE unless changed.
