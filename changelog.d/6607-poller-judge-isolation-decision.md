<!-- changelog: changed -->
- **The docs now say which sandbox the orchestrator poller judges use, and a contract test keeps that from changing unnoticed.** Runtime behaviour is unchanged.

The five poller judges (wave, stall, integration, security-pass and review-blocked) run in the review sandbox (`scripts/review_untrusted_sandbox.sh`), not in `scripts/codex_isolated_exec.sh`. `agents.md` and `README.md` said otherwise; both now match the code. For these judges the codex engine means OpenCode in a fresh sandbox. A sandbox failure defers the judge and never falls back to the host. Failures log under `JUDGE_ISOLATION` / `RB_JUDGE_ISOLATION`, not `CODEX_ISOLATION`. The two isolation stacks are maintained separately, so a fix to one does not fix the other. `test_poller_judges_stay_on_the_review_sandbox` fails if a judge moves to another launcher.

The merged-PR guard (`.claude/hooks/pr_merge_status_guard.py`) is still excluded from the review sandbox, because it runs on the host with credentials. Review autofix therefore cannot repair a CI failure in that hook. Fix it in an interactive session, and change the hook and its `workflow-templates/.claude/hooks/` copy together.
