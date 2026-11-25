# Deploy-Activation Log — ChatGPT account reviewer trial (review panel GPT slot on ChatGPT accounts via claude-pool-broker)

- Reference: #8969 (orchestrator tracking issue, closed)   (+ plan docs/plans/chatgpt-account-reviewer-trial-plan.md, PR #8971)
- Deploy target: shubhodeep1/coding-workflows (+ claude-pool-broker Worker on FT_GAMES_CF, shubhodeep1/claude-workers refresh workflow)
- How it runs: pull_request review (`.github/workflows/review_autofix.yml`) once the slot switch is on; accounts kept fresh by claude-workers `chatgpt-pool-refresh.yml` (cron `41 */4 * * *` from its default branch)
- Status: IN_PROGRESS
- Last updated: 2026-11-25
- Last note: Step 1 verified already done; Step 2 waits on the owner adding CHATGPT_POOL_AUTH_<NAME> secrets in shubhodeep1/claude-workers (latest refresh run 45521472309 logged `accounts=0`).
- Human-steps issue: https://github.com/shubhodeep1/coding-workflows/issues/9647 — keys project-8969

## Runbook
1. [x] Deploy claude-pool-broker Worker with the ChatGPT routes and Durable Object — covers project-8969 «Deploy the claude-pool-broker Worker with the ChatGPT routes and Durable Object» — done 2026-11-25: already configured, verified. Live Worker has the `CHATGPT_ACCOUNTS` durable_object_namespace binding and `CHATGPT_GATE` / `CHATGPT_WRITER_REPOSITORY` / `CHATGPT_WRITER_WORKFLOW` vars; last code upload 2026-11-21T01:26Z (version 5e1955cb), after the last broker commit on main (efee99ef, 2026-11-18, the entry's source SHA); later versions are secret-triggered only. `GET /v1/chatgpt/status` without a token answers `403 missing_token` (an unknown route answers `404 not_found`), so the routes are live. No redeploy needed.
2. [ ] Load ChatGPT accounts into the broker — covers project-8969 «Load ChatGPT accounts into the broker through the claude-workers refresh workflow». Workflow `chatgpt-pool-refresh.yml` already exists in shubhodeep1/claude-workers (cron `41 */4 * * *`, `id-token: write`, audience `coding-workflows-chatgpt-pool-writer`) and succeeds, but logs `CHATGPT_POOL refresh accounts=0`: no `CHATGPT_POOL_AUTH_<NAME>` secret is set. Remaining: owner saves each account's `auth.json` as a secret (claude-workers README "Adding a ChatGPT account"), runs the workflow, and `/v1/chatgpt/status` lists each account `usable=true`.
3. [ ] Turn on the ChatGPT reviewer slot (go-live switch) — covers project-8969 «Turn on the ChatGPT reviewer slot (go-live switch)»
4. [ ] Verify LIVE: next PR review logs `REVIEWER_CHATGPT: resolve slot=true` and `CHATGPT_POOL available=true`

## Notes
- 2026-11-25: the session's local clone is shallow, so `git log` shows #9702 as adding the whole broker directory; the GitHub commits API shows the last broker change on main is efee99ef (#8971).
