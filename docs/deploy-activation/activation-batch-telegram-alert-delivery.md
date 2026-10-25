# Deploy-Activation Log — Telegram alert delivery (shared operator step, batch)

- Reference: https://github.com/shubhodeep1/coding-workflows/issues/6211, shared step "Configure Telegram alert delivery"   (+ entries pr-6462, pr-6951, pr-6655, pr-7156, pr-7176, pr-7201, pr-6980 — all PRs in shubhodeep1/coding-workflows, all merged 2026-10-07..2026-10-12)
- Deploy target: shubhodeep1/coding-workflows (this repo only; consumer repos out of scope per operator answer Q1: A)
- How it runs: cron from default branch (orchestrate poller) + pull_request (review, check-failure triage); alerts sent by scripts/tg_helpers.sh, which skips silently when TG_BOT_SECRET or TG_ADMIN_CHAT_ID is empty
- Status: IN_PROGRESS
- Last updated: 2026-10-25
- Last note: Run logs show Telegram already configured; Step 1 (operator confirms settings page) emitted.

## Runbook
1. [ ] Confirm in repo Settings → Secrets and variables → Actions: TG_BOT_SECRET secret present, TG_ADMIN_CHAT_ID variable present, ALERT_MSG_LEVEL absent or WARNING
2. [ ] Tick the Telegram checkbox in each of the 7 entry comments on #6211 (pr-7176 with note: DEBUG declined)
3. [ ] Verify LIVE (re-read #6211 entries and a fresh poller run log)

## Notes
- 2026-10-25 pre-check (read-only, from run logs):
  - orchestrate_poll run 40160712340 (schedule, 2026-10-25 04:07 UTC): TG_BOT_SECRET=*** (set), TG_ADMIN_CHAT_ID set, ALERT_MSG_LEVEL=WARNING.
  - internal-check-failure-triage run 40108741912: same three values.
  - internal-review.yml calls review_autofix.yml with `secrets: inherit`, so review gets the same repo secret.
  - Direct reads of repo secrets/variables are refused by the web-session proxy (HTTP 403), so the settings page check is Step 1.
- Q2: A — ALERT_MSG_LEVEL stays WARNING (pr-7156 intent). pr-7176 asked for DEBUG to deliver gated-only Claude pool notices (scripts/claude_pool_health_alert.sh:141-145); operator declined because the variable is repo-wide and DEBUG would page on every DEBUG alert. Rejected-token / failed-probe notices still send at WARNING.
- Only the Telegram checkbox of each entry is in scope; the entries' other steps are handled in their own sessions.
